from __future__ import annotations

from typing import Any
import logging
import random
import html
import discord
from discord import app_commands, ui
from discord.ext import commands
import requests

TRIVIA_URL = "https://opentdb.com/api.php?amount=1&category=18&type=multiple"

log = logging.getLogger("bot")

def decode_html(text: str) -> str:
    return html.unescape(text)

class TriviaView(ui.View):
    def __init__(self, correct_answer: str, answers: list[str]):
        super().__init__(timeout=60)
        self.correct_answer = correct_answer
        self.answers = answers
        self.answered = False

        for i, answer in enumerate(answers):
            # Truncate answer for button (Discord limit = 80 chars)
            button_label = answer[:77] + "..." if len(answer) > 80 else answer
            
            button = ui.Button(
                label=button_label,
                style=discord.ButtonStyle.primary,
                custom_id=f"answer_{i}"
            )
            button.callback = self.create_callback(answer)
            self.add_item(button)

    def create_callback(self, selected_answer: str):
        async def callback(interaction: discord.Interaction):
            if self.answered:
                await interaction.response.send_message("You already answered!", ephemeral=True)
                return

            self.answered = True
            self.disable_all_buttons()

            is_correct = selected_answer == self.correct_answer

            if is_correct:
                embed = discord.Embed(title="✅ Correct Answer!", color=discord.Color.green())
            else:
                embed = discord.Embed(
                    title="❌ Wrong Answer",
                    description=f"The correct answer was:\n**{self.correct_answer}**",
                    color=discord.Color.red()
                )

            await interaction.response.edit_message(embed=embed, view=self)

        return callback

    def disable_all_buttons(self):
        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True


class Trivia(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="trivia", description="Play a science & computers trivia question")
    @app_commands.checks.cooldown(3, 10.0, key=lambda i: i.user.id)
    async def trivia(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            res = requests.get(TRIVIA_URL, timeout=10)
            res.raise_for_status()
            data: dict[Any, Any] = res.json()

            if not data.get("results"):
                await interaction.followup.send("❌ Failed to fetch trivia.")
                return

            q = data["results"][0]

            question_text = decode_html(q['question'])
            correct = decode_html(q['correct_answer'])
            incorrects = [decode_html(ans) for ans in q['incorrect_answers']]

            all_answers = [correct] + incorrects
            random.shuffle(all_answers)

            embed = discord.Embed(
                title="🧠 Trivia Question",
                description=question_text,
                color=discord.Color.blue()
            )
            embed.set_footer(text=f"Category: {q.get('category')} | 60 seconds to answer")

            view = TriviaView(correct_answer=correct, answers=all_answers)

            await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            log.error(f"Trivia command failed: {e}", exc_info=True)
            await interaction.followup.send("❌ An error occurred while fetching the question.")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Trivia(bot))
