from interactions import Client, Intents, listen
from interactions import slash_command, slash_option, SlashContext, OptionType
from interactions import Button, ButtonStyle, ActionRow
from interactions.api.events import Component
import interactions

import configparser
import yaml

import httpx

import asyncio
import re

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



flow_data = {}
async def load_data():
    repo = config['bot']['REPO']
    online = config['bot']['ONLINE']

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
    
    for page in flow_data.values():
        text = page["text"].strip()
        text = IMG_LINKS.sub(rf"[\1](<{repo}i/\2.png>)", text)
        text = SITE_LINKS.sub(rf"](<{online}\1>)", text)
        text = REG_LINKS.sub(rf"](<\1>)", text)
        page["text"] = text

async def make_flow_message(message: interactions.Message, link: str = "index"):
    l = link[1:] if link[0] == "#" else link
    l = UNSAFE_CHARS.sub("", l)
    page = flow_data.get(l, flow_data["default"])
    
    short: bool = "short" in page
    text: str = "## " + page["title"] + "\n" + page.get("short", page["text"])
    if len(text) > 1950:
        space = max(text.rfind(" ", 0, 1850), text.rfind("\t", 0, 1850), text.rfind("\r", 0, 1850), text.rfind("\n", 0, 1850))
        if space < 500: space = 1850
        text = text[:space] + "..."
        short = True
    
    components = []
    for opt in page.get("opts", []):
        components.append(Button(style=ButtonStyle.GRAY, label=opt["text"], custom_id=opt["link"]))
    
    if len(components) > 25:
        end = components[-1]
        components = components[:23]
        components.append(end)
        components.append(Button(style=ButtonStyle.BLUE, label="More online...", custom_id="more-online"))
        short = True

    if short:
        text += f"\n\nTo read more, see the [#{l}](<{config['bot']['ONLINE']}#{l}>) page online."
    
    await message.edit(content=text, components=ActionRow.split_components(*components) if components else [])

@listen(Component)
async def on_component(event: Component):
    ctx = event.ctx
    await ctx.defer(edit_origin=True)

    if ctx.custom_id == "more-online":
        pass
    elif ctx.message is not None:
        await make_flow_message(ctx.message, ctx.custom_id)
    


@listen()
async def on_ready():
    print("Bot started")

@slash_command(description="Launches the Flow Help bot.")
@slash_option(
    name = "start",
    description = "Optional place to start from in the flow help.",
    opt_type = OptionType.STRING
)
async def flowhelp(ctx: SlashContext, start: str = "index"):
    await make_flow_message(await ctx.send("Thinking..."), start.strip())

@slash_command(description="ping pong")
async def ping(ctx: SlashContext):
    await ctx.send("pong")



def main():
    asyncio.run(load_data())
    print("Flow data loaded")
    bot.start()

if __name__ == "__main__":
    main()