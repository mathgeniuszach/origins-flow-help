# this file is the flow bot, in all of it's code.

from interactions import Client, Intents, listen, Permissions
from interactions import slash_command, slash_option, slash_default_member_permission, SlashContext, OptionType
from interactions import Button, ButtonStyle, ActionRow
from interactions.api.events import Component
import interactions

import configparser
import yaml

import httpx

import asyncio
import re
import signal
import traceback

UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
IMG_LINKS = re.compile(r"!\[(.*?)\]\((.*?)\)")
REG_LINKS = re.compile(r"\]\((?!<)([^\)]+)\)")
SITE_LINKS = re.compile(r"\]\((?!<)(#[^\)]+)\)")

SESSION_LOCK = asyncio.Lock()


config = configparser.ConfigParser()
config.read("config.ini")

# bot = None
bot = Client(
    intents = Intents.ALL,
    token = config['bot']['TOKEN'],
    default_scope = config['bot']['GUILD']
)

sessions = {}
flow_data = {}
async def load_data(local=""):
    global flow_data, meta_map, meta_start
    repo = config['bot']['REPO']
    online = config['bot']['ONLINE']

    if local:
        with open(local+"index.yaml", "r") as f:
            index = yaml.safe_load(f)
        flow_data["default"] = index["default"]

        for file in index['data']:
            with open(local+file, "r") as f:
                flow_data.update(yaml.safe_load(f))
    else:
        raise NotImplementedError("A local directory must be provided")
        # Download content
        # async with httpx.AsyncClient(follow_redirects=True) as http_client:
        #     try:
        #         resp = await http_client.get(repo + "index.yaml")
        #         index = yaml.safe_load(resp.content)
        #         flow_data["default"] = index["default"]

        #         async def load_file(file):
        #             try:
        #                 resp = await http_client.get(repo + file)
        #                 flow_data.update(yaml.safe_load(resp.content))
        #             except httpx.ReadError as e:
        #                 print(f'ERROR: Failed to download "{repo}{file}"; {e.__class__.__name__}: {str(e)}')
        #         for file in index['data']:
        #             await load_file(file) # Should be task group but unsupported in 3.10
        #     except httpx.ReadError as e:
        #         print(f'ERROR: Failed to download "{repo}index.yaml"; {e.__class__.__name__}: {str(e)}')
    
    # Parse links
    # long = []
    for key, page in flow_data.items():
        text = page.get("short", page["text"]).strip()
        text = IMG_LINKS.sub(rf"[IMAGE: \1](<{repo}i/\2.png>)", text)
        text = SITE_LINKS.sub(rf"](<{online}\1>)", text)
        text = REG_LINKS.sub(rf"](<\1>)", text)
        page["text"] = text

        # if len(text) > 1800:
        #     long.append((len(text), key))
    
    # long.sort()
    # for l, key in long:
    #     print(f'Warning: page "{key}" is {l} chars long')

class Session:
    def __init__(self, ctx: SlashContext, message: interactions.Message, back_enabled: bool = True, mod_only: bool = False):
        self.ctx = ctx
        self.message = message
        self.age = 0

        self.buttons = []
        self.start = 0
        self.prev = []
        self.link = ""
        self.mod_only = mod_only
        self.back_enabled = back_enabled
    
    async def load(self, link: str):
        l = link.strip()
        l = l[1:] if l[0] == "#" else l
        l = UNSAFE_CHARS.sub("", l)

        if self.link and self.back_enabled:
            self.prev.append(self.link)
        self.link = l
        page = flow_data.get(l, flow_data["default"])
        
        short: bool = "short" in page
        text: str = f"## {page['title']} ({self.link})\n{page['text']}"
        if short:
            text += f"\n\nTo read more, see the [#{l}](<{config['bot']['ONLINE']}#{l}>) page online."
        
        if len(text) > 1950:
            space = max(text.rfind(" ", 0, 1800), text.rfind("\t", 0, 1800), text.rfind("\r", 0, 1800), text.rfind("\n", 0, 1800))
            if space < 500: space = 1800
            text = text[:space] + "..."
            text += f"\n\nTo read more, see the [#{l}](<{config['bot']['ONLINE']}#{l}>) page online."
        
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
        if self.buttons:
            await self.message.edit(components=[])
            del sessions[self.message.id]

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
                    await s.close()



async def on_exit():
    await close_sessions()
    await bot.stop()

@listen()
async def on_startup():
    print("Bot started")

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(on_exit()))
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(on_exit()))

    asyncio.create_task(timeout_sessions())

async def _make_session(ctx: SlashContext, start: str = "index", back: bool = True, modonly: bool = False, mentions: interactions.Member | None = None):
    async with SESSION_LOCK:
        while len(sessions) >= int(config['session']['MAX_SESSIONS']):
            await sessions.pop(list(sessions.keys())[0]).close()

        message = await ctx.send("Thinking...")
        if mentions is not None:
            await ctx.send(mentions.mention)
        
        s = Session(ctx, message, back, modonly)
        sessions[message.id] = s
    await s.load(start)

@slash_command(description="Launches the Flow Help bot.")
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
    await load_data(config['bot']['LOCAL'])
    await ctx.send("Reloaded Flow Help.", ephemeral=True)

@slash_command(description="ping pong")
async def ping(ctx: SlashContext):
    await ctx.send("pong")



def main():
    asyncio.run(load_data(config['bot']['LOCAL']))
    print("Flow data loaded")
    bot.start()

if __name__ == "__main__":
    main()