await interaction.response.send_message(
            "⚠️ L'image doit utiliser une URL commençant par `https://` "
            "ou `http://`.",
            ephemeral=True,
        )
        return

    # --------------------------------------------------------
    # STYLE IMMERSIF
    # Les titres Markdown sont affichés en grand dans Discord.
    # --------------------------------------------------------
    if style == "immersive":
        content_parts = [
            f"# {titre}",
        ]

        if sous_titre:
            content_parts.append(f"## {sous_titre}")

        content_parts.append(texte)

        if image_url:
            content_parts.append(image_url)

        content = "\n\n".join(content_parts)

        # Discord limite le contenu d'un message à 2000 caractères.
        if len(content) > 2000:
            await interaction.response.send_message(
                "⚠️ Le message immersif dépasse la limite de 2 000 "
                "caractères de Discord. Raccourcis le titre, le sous-titre "
                "ou le texte, puis réessaie.",
                ephemeral=True,
            )
            return

        try:
            await channel.send(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Je n'ai pas la permission d'envoyer des messages "
                "dans le salon worlds-2026.",
                ephemeral=True,
            )
            return

        except discord.HTTPException as error:
            logger.exception("Erreur de publication immersive : %s", error)

            await interaction.response.send_message(
                "❌ Discord n'a pas accepté la publication. "
                "Vérifie la longueur du message et les permissions du bot.",
                ephemeral=True,
            )
            return

    # --------------------------------------------------------
    # STYLE EMBED
    # --------------------------------------------------------
    else:
        embed = discord.Embed(
            title=titre,
            description=texte[:4096],
            color=selected_color,
            timestamp=datetime.now(timezone.utc),
        )

        if sous_titre:
            embed.set_author(name=sous_titre)

        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(text="WORLDS HUB • Worlds 2026")

        try:
            await channel.send(embed=embed)

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Je n'ai pas la permission d'envoyer des messages "
                "dans le salon worlds-2026.",
                ephemeral=True,
            )
            return

        except discord.HTTPException as error:
            logger.exception("Erreur de publication embed : %s", error)

            await interaction.response.send_message(
                "❌ Discord n'a pas accepté la publication. "
                "Vérifie les champs et les permissions du bot.",
                ephemeral=True,
            )
            return

    # Confirmation privée
    await interaction.response.send_message(
        f"✅ Annonce publiée dans {channel.mention} "
        f"avec le style **{style}**.",
        ephemeral=True,
    )


# ============================================================
# DEMARRAGE
# ============================================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "La variable d'environnement DISCORD_TOKEN est manquante."
        )

    bot.run(TOKEN)
