import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os

load_dotenv()

token = os.getenv("DISCORD_TOKEN")

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True

bot = commands.Bot(command_prefix='!', intents=intents)

ROLE = "Peasant"

@bot.event
async def on_ready():
    print(f"We are ready to go in, {bot.user.name}")
    
@bot.event
async def on_member_join(member: discord.member.Member):
    await member.send(f"Welcome to the server {member.name}")

@bot.event
async def on_message(message: discord.message.Message):
    if message.author == bot.user:
        return

    if "shit" in message.content.lower():
        await message.delete()
        await message.channel.send(f"{message.author.mention} - don't use that word!")
        
    await bot.process_commands(message)

@bot.command()
async def hello(ctx: commands.Context):
    await ctx.send(f"Hello {ctx.author.mention}!")
    
@bot.command()
async def assign(ctx: commands.Context):
    role = discord.utils.get(ctx.guild.roles, name=ROLE)
    if role:
        await ctx.author.add_roles(role)
        await ctx.send(f"{ctx.author.mention} is now assigned to {ROLE}")
    else:
        await ctx.send("Role doesn't exist")
        
@bot.command()
async def remove(ctx: commands.Context):
    role = discord.utils.get(ctx.guild.roles, name=ROLE)
    if role:
        await ctx.author.remove_roles(role)
        await ctx.send(f"{ctx.author.mention} has had the {ROLE} removed")
    else:
        await ctx.send("Role doesn't exist")
    
@bot.command()
@commands.has_role(ROLE)
async def secret(ctx: commands.Context):
    await ctx.send("Welcome to the club!")
    
@bot.command()
async def dm(ctx: commands.Context, *, msg):
    try:
        await ctx.author.send(f"You said {msg}")
        await ctx.send("DM sent!")
    except discord.Forbidden:
        await ctx.send("I can't send you DMs.")
    
@bot.command()
async def reply(ctx: commands.Context):
    await ctx.reply("This is a reply to your message!")
    
@bot.command()
async def poll(ctx: commands.Context, *, question):
    embed = discord.Embed(title="New Poll", description=question)
    poll_message = await ctx.send(embed=embed)
    await poll_message.add_reaction("👍")
    await poll_message.add_reaction("👎")
    
@secret.error
async def secret_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingRole):
        await ctx.send("You do not have permission to do that!")
    else:
        await ctx.send("Something went wrong!")
    
def main():
    bot.run(token, log_handler=handler, log_level=logging.DEBUG)
    
if __name__ == "__main__":
    main()
