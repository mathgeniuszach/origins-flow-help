# this file is the flow bot, in all of it's code.

from interactions import Client, Intents, listen, Permissions
from interactions import slash_command, slash_option, slash_default_member_permission, SlashContext, OptionType
from interactions import Button, ButtonStyle, ActionRow
from interactions.api.events import Component
import interactions

import configparser
import yaml

import requests

import asyncio
import re
import signal
import logging

from sys import stdout, argv
from os import unlink, mkdir, rename
from shutil import rmtree
from os.path import exists, join, abspath, dirname
from zipfile import ZipFile
from io import BytesIO
from tempfile import TemporaryDirectory

LOGGER = logging.getLogger("flow-bot")
LOG_FORMAT = "[{asctime}] {levelname:<8}  {message}"

def setup_logger():
    if exists(config['log']['LOG_FILE']):
        unlink(config['log']['LOG_FILE'])
    
    class NullWriter:
        def write(self, _): pass
    
    logging.basicConfig(stream=NullWriter(), format=LOG_FORMAT, datefmt="%I:%M:%S", style="{", level=0)

    log_formatter = logging.Formatter(LOG_FORMAT, datefmt="%I:%M:%S", style="{")

    log_handler_file = logging.FileHandler(config['log']['LOG_FILE'])
    log_handler_file.setLevel(config['log']['LOG_LEVEL_FILE'])
    log_handler_file.setFormatter(log_formatter)
    LOGGER.addHandler(log_handler_file)

    log_handler_out = logging.StreamHandler(stdout)
    log_handler_out.setLevel(config['log']['LOG_LEVEL_STDOUT'])
    log_handler_out.setFormatter(log_formatter)
    LOGGER.addHandler(log_handler_out)

UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
IMG_LINKS = re.compile(r"!\[(.*?)\]\((.*?)\)")
REG_LINKS = re.compile(r"\]\((?!<)([^\)]+)\)")
SITE_LINKS = re.compile(r"\]\((?!<)(#[^\)]+)\)")

SESSION_LOCK = asyncio.Lock()

MAX_LINK_CHARS = 100
DISCORD_MAX_CHARS = 2000

CONFIG_PATH = abspath(join(dirname(__file__), "config.ini") if len(argv) < 2 else argv[1])

def resolve(path: str):
    return abspath(join(dirname(CONFIG_PATH), path))

config = configparser.ConfigParser()
config.read(CONFIG_PATH)

# bot = None
bot = Client(
    intents = Intents.ALL,
    token = config['bot']['TOKEN'],
    default_scope = config['bot']['GUILD']
)

sessions = {}
flow_data = {}
async def load_data(local, download):
    global flow_data, meta_map, meta_start
    repo = config['bot']['REPO']
    online = config['bot']['ONLINE']
    
    if download or not exists(local):
        with requests.get(config['bot']['ZIP']) as resp:
            zfile = ZipFile(BytesIO(resp.content))
            
            if exists(local): rmtree(local)
            mkdir(local)
            
            zfile.extractall(local)
            folder = zfile.namelist()[0]
            for f in ("i", "data", "index.yaml"):
                rename(join(local, folder, f), join(local, f))
            rmtree(join(local, folder))

    with open(join(local, "index.yaml"), "r") as f:
        index = yaml.safe_load(f)
    flow_data["default"] = index["default"]

    for file in index['data']:
        with open(join(local, file), "r") as f:
            flow_data.update(yaml.safe_load(f))
    
    # Parse links
    long = []
    for link, page in flow_data.items():
        text: str = page.get("short", page["text"]).strip()
        text = IMG_LINKS.sub(rf"[IMAGE: \1](<{repo}i/\2.png>)", text)
        text = SITE_LINKS.sub(rf"](<{online}\1>)", text)
        text = REG_LINKS.sub(rf"](<\1>)", text)

        if link == "default":
            max_len_str = f"## {page['title']} ({'x'*MAX_LINK_CHARS})\n{text}"
            if len(max_len_str) > DISCORD_MAX_CHARS:
                text = "ERROR: default page too long"
                LOGGER.error("default page can be %d chars long", len(max_len_str))
            page[text] = text
            continue

        text = f"## {page['title']} ({link})\n{text}"
        extra = f"\n\nTo read more, see the [#{link}](<{config['bot']['ONLINE']}#{link}>) page online."
        if "short" in page:
            text += extra
        
        if len(text) > DISCORD_MAX_CHARS:
            long.append(("short" in page, link, len(text)))

            start_trunc = DISCORD_MAX_CHARS - len(extra) - 1
            space = max(
                text.rfind(" ", 0, start_trunc),
                text.rfind("\t", 0, start_trunc),
                text.rfind("\r", 0, start_trunc),
                text.rfind("\n", 0, start_trunc)
            )
            if space < 500: space = start_trunc

            text = text[:space] + "..."
            text += extra
        
        page["text"] = text
    
    long.sort()
    for short, link, chars in long:
        if short:
            LOGGER.warn('short page "%s" is %d chars long', link, chars)
        else:
            LOGGER.warn('page "%s" is %d chars long', link, chars)

class Session:
    def __init__(self, ctx: SlashContext, message: interactions.Message, back_enabled: bool = True, mod_only: bool = False):
        self.ctx = ctx
        self.message = message
        self.id = message.id
        self.age = 0

        self.buttons = []
        self.start = 0
        self.prev = []
        self.link = ""
        self.mod_only = mod_only
        self.back_enabled = back_enabled

        LOGGER.debug("session %s created", str(self.id))
    
    async def load(self, link: str):
        l = link.strip()
        if len(l) > 100: l = l[:100]
        if l[0] == "#": l = l[1:]
        l = UNSAFE_CHARS.sub("", l)

        if self.link and self.back_enabled:
            self.prev.append(self.link)
        self.link = l
        page = flow_data.get(l)

        if page is None:
            page = flow_data["default"]
            text: str = f"## {page['title']} ({l})\n{page['text']}"
        else:
            text: str = page["text"]
        
        self.buttons = []
        for opt in page.get("opts", []):
            self.buttons.append(Button(style=ButtonStyle.GRAY, label=opt["text"], custom_id=opt["link"]))
        components = self.buttons.copy()

        m = 24 if self.prev else 25
        if len(components) > m:
            if self.start > len(components): self.start = 0

            end = min(self.start+m-1, len(components))
            components = components[self.start:end]
            components.append(Button(style=ButtonStyle.BLUE, label="More...", custom_id="more-button"))
        
        if self.prev:
            components.append(Button(style=ButtonStyle.RED, label="Back", custom_id="back-button"))
        
        await self.message.edit(content=text, components=ActionRow.split_components(*components) if components else [])

        if not components:
            await self.close()

    async def back(self):
        if not self.prev: return
        self.link = ""
        self.start = 0
        await self.load(self.prev.pop())
    
    async def more(self):
        link = self.link
        self.link = ""
        self.start += 23 if self.prev else 24
        await self.load(link) # Not technically necessary, but I'm lazy
    
    async def close(self):
        try:
            await self.message.edit(components=[])
        except Exception:
            LOGGER.warn("close edit failed on message %s", str(self.id))
        
        try:
            if self.id in sessions:
                del sessions[self.id]
        except Exception:
            LOGGER.warn("close delete failed on message %s", str(self.id))

        LOGGER.debug("session %s closed", str(self.id))

@listen(Component)
async def on_component(event: Component):
    ctx = event.ctx
    if ctx.message is None: return

    message = ctx.message

    await ctx.defer(edit_origin=True)

    async with SESSION_LOCK:
        s = sessions.get(message.id)
        if s is None:
            # Broken state, typically caused by improper shutdown.
            await message.edit(components=[])
            return
        
    if s.mod_only and (ctx.member is None or not ctx.member.has_permission(Permissions.MANAGE_MESSAGES)):
        return
    
    match ctx.custom_id:
        case 'more-button':
            await s.more()
        case 'back-button':
            await s.back()
        case link:
            s.start = 0
            await s.load(link)

async def close_sessions():
    LOGGER.info("closing all %d sessions", len(sessions))
    async with SESSION_LOCK:
        for s in list(sessions.values()):
            await s.close()
    
async def timeout_sessions():
    delay = int(config['session']['AGE_TIMER'])
    max_age = int(config['session']['MAX_AGE'])
    while True:
        await asyncio.sleep(delay)
        async with SESSION_LOCK:
            for s in list(sessions.values()):
                s.age += 1
                if s.age > max_age:
                    LOGGER.debug("session %s timeouted", str(s.id))
                    await s.close()



async def on_exit():
    LOGGER.info("exit signal received")
    await close_sessions()
    await bot.stop()

@listen()
async def on_startup():
    LOGGER.info("bot started")

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(on_exit()))
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(on_exit()))

    asyncio.create_task(timeout_sessions())

async def _make_session(ctx: SlashContext, start: str = "index", back: bool = True, modonly: bool = False, mentions: interactions.Member | None = None):
    async with SESSION_LOCK:
        while len(sessions) >= int(config['session']['MAX_SESSIONS']):
            LOGGER.warn("max sessions reached, closing an old session")
            await sessions.pop(list(sessions.keys())[0]).close()

        message = await ctx.send("Thinking...")
        if mentions is not None:
            await ctx.send(mentions.mention)
        
        s = Session(ctx, message, back, modonly)
        sessions[message.id] = s
    await s.load(start if len(start) <= MAX_LINK_CHARS else start[:MAX_LINK_CHARS])

@slash_command(description="Launches the Flow Help bot.")
# @slash_default_member_permission(Permissions.MANAGE_MESSAGES)
@slash_option(
    name = "start",
    description = "Optional place to start from in the flow help. Defaults to index",
    opt_type = OptionType.STRING
)
@slash_option(
    name = "back",
    description = "Whether the back button is shown. Defaults to True.",
    opt_type = OptionType.BOOLEAN
)
@slash_option(
    name = "modonly",
    description = "Whether buttons are only available to moderators. Defaults to False.",
    opt_type = OptionType.BOOLEAN
)
@slash_option(
    name = "mentions",
    description = "An optional user to ping.",
    opt_type = OptionType.USER
)
async def flowhelp(ctx: SlashContext, start: str = "index", back: bool = True, modonly: bool = False, mentions: interactions.Member | None = None):
    await _make_session(ctx, start, back, modonly, mentions)

@slash_command(description="Launches the Flow Help bot but with back=False and modonly=True.")
# @slash_default_member_permission(Permissions.MANAGE_MESSAGES)
@slash_option(
    name = "start",
    description = "Optional place to start from in the flow help. Defaults to index",
    opt_type = OptionType.STRING
)
@slash_option(
    name = "mentions",
    description = "An optional user to ping.",
    opt_type = OptionType.USER
)
async def flowshow(ctx: SlashContext, start: str = "index", mentions: interactions.Member | None = None):
    await _make_session(ctx, start, False, True, mentions)

@slash_command(description="Reloads the Flow Help bot.")
@slash_default_member_permission(Permissions.ADMINISTRATOR)
async def reflow(ctx: SlashContext):
    await close_sessions()
    await load_data(resolve(config['bot']['LOCAL']), True)
    await ctx.send("Reloaded Flow Help.", ephemeral=True)
    LOGGER.info("flow data reloaded")

@slash_command(description="ping pong")
async def ping(ctx: SlashContext):
    await ctx.send("pong")



def main():
    setup_logger()
    asyncio.run(load_data(resolve(config['bot']['LOCAL']), False))
    LOGGER.info("flow data loaded")
    bot.start()

if __name__ == "__main__":
    main()