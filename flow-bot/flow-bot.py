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
from os.path import exists

UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
IMG_LINKS = re.compile(r"!\[(.*?)\]\((.*?)\)")
REG_LINKS = re.compile(r"\]\((?!<)([^\)]+)\)")
SITE_LINKS = re.compile(r"\]\((?!<)(#[^\)]+)\)")

config = configparser.ConfigParser()
config.read("config.ini")


bot = Client(
    intents = Intents.ALL,
    token = config['bot']['TOKEN'],
    default_scope = config['bot']['GUILD']
)

sessions = {}
flow_data = {}
async def load_data():
    global flow_data, meta_map, meta_start
    repo = config['bot']['REPO']
    online = config['bot']['ONLINE']

    # Download content
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.get(repo + "index.yaml")
        index = yaml.safe_load(resp.content)
        flow_data["default"] = index["default"]

        async with asyncio.TaskGroup() as tg:
            async def load_file(file):
                resp = await http_client.get(repo + file)
                flow_data.update(yaml.safe_load(resp.content))

            for file in index['data']:
                tg.create_task(load_file(file))
    
    # Parse links
    for page in flow_data.values():
        text = page["text"].strip()
        text = IMG_LINKS.sub(rf"[IMAGE: \1](<{repo}i/\2.png>)", text)
        text = SITE_LINKS.sub(rf"](<{online}\1>)", text)
        text = REG_LINKS.sub(rf"](<\1>)", text)
        page["text"] = text

class Session:
    def __init__(self, ctx: SlashContext, message: interactions.Message):
        self.ctx = ctx
        self.message = message
        self.age = 0

        self.buttons = []
        self.start = 0
        self.prev = []
        self.link = ""
    
    async def load(self, link: str):
        l = link.strip()
        l = l[1:] if l[0] == "#" else l
        l = UNSAFE_CHARS.sub("", l)

        if self.link:
            self.prev.append(self.link)
        self.link = l
        page = flow_data.get(l, flow_data["default"])
        
        short: bool = "short" in page
        text: str = f"## {page['title']} ({self.link})\n{page.get('short', page['text'])}"
        if len(text) > 1950:
            space = max(text.rfind(" ", 0, 1850), text.rfind("\t", 0, 1850), text.rfind("\r", 0, 1850), text.rfind("\n", 0, 1850))
            if space < 500: space = 1850
            text = text[:space] + "..."
            short = True
        
        if short:
            text += f"\n\nTo read more, see the [#{l}](<{config['bot']['ONLINE']}#{l}>) page online."
        
        self.buttons = []
        for opt in page.get("opts", []):
            self.buttons.append(Button(style=ButtonStyle.GRAY, label=opt["text"], custom_id=opt["link"]))
        components = self.buttons

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
        await self.load(self.prev.pop())
    
    async def more(self):
        self.link = ""
        self.start += 23 if self.prev else 24
        await self.load(self.link) # Not technically necessary, but I'm lazy
    
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

    s = sessions.get(message.id)
    if s is None:
        # Broken state, typically caused by improper shutdown.
        await message.edit(components=[])
        return
    
    match ctx.custom_id:
        case 'more-button':
            await s.more()
        case 'back-button':
            await s.back()
        case link:
            await s.load(link)

async def close_sessions():
    for s in sessions.values():
        await s.close()
    
async def timeout_sessions():
    delay = int(config['session']['AGE_TIMER'])
    max_age = int(config['session']['MAX_AGE'])
    while True:
        await asyncio.sleep(delay)
        for s in list(sessions.values()):
            s.age += 1
            if s.age > max_age:
                s.close()

@listen()
async def on_ready():
    print("Bot started")

    asyncio.create_task(timeout_sessions())

@slash_command(description="Launches the Flow Help bot.")
@slash_option(
    name = "start",
    description = "Optional place to start from in the flow help.",
    opt_type = OptionType.STRING
)
async def flowhelp(ctx: SlashContext, start: str = "index"):
    while len(sessions) >= int(config['session']['MAX_SESSIONS']):
        await sessions.pop(list(sessions.keys())[0]).close()

    message = await ctx.send("Thinking...")
    s = Session(ctx, message)
    sessions[message.id] = s
    await s.load(start)

@slash_command(description="Reloads the Flow Help bot.")
@slash_default_member_permission(Permissions.ADMINISTRATOR)
async def reflow(ctx: SlashContext):
    await close_sessions()
    await load_data()

@slash_command(description="ping pong")
async def ping(ctx: SlashContext):
    await ctx.send("pong")



def main():
    asyncio.run(load_data())
    print("Flow data loaded")
    try:
        bot.start()
    except KeyboardInterrupt:
        asyncio.run(close_sessions())

if __name__ == "__main__":
    main()