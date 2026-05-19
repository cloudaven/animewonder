"""
Anime style packs. Each pack supplies:
  - image_suffix : appended to every scene image prompt (for Pollinations/Seedream)
  - vibe_hint    : passed to Claude so dialogue/action tone matches the style
  - badge        : 2-3 char label used in the UI
"""

STYLES = {
    "solo_leveling": {
        "name": "Solo Leveling",
        "badge": "SL",
        "image_suffix": (
            "Solo Leveling anime art style, A-1 Pictures quality, dark fantasy dungeon aesthetic, "
            "dramatic cinematic lighting, deep shadows, glowing blue mana aura, ultra-detailed, "
            "4K resolution, hyper-detailed background, volumetric light rays, epic composition"
        ),
        "vibe_hint": "Dark power-fantasy. Stoic protagonist, mana auras, dungeon settings, awakening arcs. Sharp blue/purple glows.",
    },
    "demon_slayer": {
        "name": "Demon Slayer",
        "badge": "DS",
        "image_suffix": (
            "Demon Slayer anime art style, ufotable quality, vivid color grading, traditional Japanese setting, "
            "intricate kimono patterns, ink-wash backgrounds, water/flame effects rendered as flowing paint, "
            "dramatic sword poses, particle-rich atmosphere, 4K, cinematic depth of field"
        ),
        "vibe_hint": "Period Japan. Family honor, demon hunters, breath-style techniques, ornate kimonos, painterly fight choreography.",
    },
    "ghibli": {
        "name": "Studio Ghibli",
        "badge": "GB",
        "image_suffix": (
            "Studio Ghibli anime art style, Miyazaki-inspired, soft watercolor backgrounds, warm natural light, "
            "lush environments, hand-painted texture, gentle hues, whimsical detail, "
            "wide pastoral compositions, painterly clouds, 4K"
        ),
        "vibe_hint": "Whimsical, nostalgic, eco-conscious. Quiet wonder over action. Slow camera, mundane magic, hand-painted feel.",
    },
    "cyberpunk": {
        "name": "Cyberpunk Anime",
        "badge": "CY",
        "image_suffix": (
            "cyberpunk anime art style, Akira / Ghost in the Shell influence, neon-drenched megacity, "
            "rain-slick streets, holographic signage, chrome implants, magenta and cyan rim light, "
            "dystopian skyline, lens flares, 4K, dense urban detail"
        ),
        "vibe_hint": "Neon-noir future. Megacorps, chrome bodies, hackers, rain on glass. Cynical, terse, neon-lit dialogue.",
    },
    "jjk": {
        "name": "Jujutsu Kaisen",
        "badge": "JK",
        "image_suffix": (
            "Jujutsu Kaisen anime art style, MAPPA quality, dark sorcery aesthetic, cursed energy effects, "
            "explosive black-and-violet auras, domain expansion visuals, modern Tokyo backdrop, "
            "kinetic action lines, dramatic shadow casting, 4K, sharp ink-style linework"
        ),
        "vibe_hint": "Modern occult. Cursed energy, domain expansions, dark humor under high stakes. Snappy banter mixed with horror beats.",
    },
    "aot": {
        "name": "Attack on Titan",
        "badge": "AT",
        "image_suffix": (
            "Attack on Titan anime art style, WIT/MAPPA quality, gritty militaristic atmosphere, "
            "ODM gear in motion, walled cities, war-torn landscapes, muted earth tones, "
            "high contrast, brutal scale, 4K, cinematic war composition"
        ),
        "vibe_hint": "Bleak military siege. Walls, titans, vertical maneuvering, conscript soldiers, moral grey-zones.",
    },
    "slice_of_life": {
        "name": "Slice of Life",
        "badge": "SL2",
        "image_suffix": (
            "modern slice-of-life anime art style, KyoAni quality, soft pastel palette, sunlit interiors, "
            "school/cafe/suburban settings, gentle expressions, clean lineart, "
            "shallow depth of field, 4K, warm cozy lighting"
        ),
        "vibe_hint": "Everyday emotional beats. School, cafes, summer afternoons. Subtle drama, gentle humor, no power fantasy.",
    },
    "dark_fantasy": {
        "name": "Dark Fantasy (Berserk)",
        "badge": "DF",
        "image_suffix": (
            "dark fantasy anime art style, Berserk-inspired, gothic medieval setting, blood-soaked battlefields, "
            "intricate armor, eldritch creatures, crosshatching texture, oppressive shadows, "
            "torchlit detail, 4K, painterly grim composition"
        ),
        "vibe_hint": "Medieval grimdark. Mercenaries, demons, cursed brands. Heavy themes, brooding monologue, visceral combat.",
    },
    "shonen_battle": {
        "name": "Shonen Battle",
        "badge": "SH",
        "image_suffix": (
            "classic shonen anime art style, Bones / Studio Pierrot quality, vibrant color palette, "
            "dynamic action poses, speed lines, explosive energy attacks, high-saturation backgrounds, "
            "expressive faces, 4K, kinetic composition"
        ),
        "vibe_hint": "Friendship-power-victory. Loud emotions, named techniques, tournament arcs, plucky underdog energy.",
    },
    "isekai": {
        "name": "Isekai Fantasy",
        "badge": "IS",
        "image_suffix": (
            "modern isekai anime art style, vibrant high-fantasy world, status-window UI overlays, "
            "guild halls and dungeons, RPG-inspired UI elements, magic-circle effects, "
            "bright fantasy palette, 4K, JRPG composition"
        ),
        "vibe_hint": "Transported-to-another-world. Status windows, level-ups, guilds, party banter, game-logic worldbuilding.",
    },
    "castlevania": {
        "name": "Castlevania (Netflix)",
        "badge": "CV",
        "image_suffix": (
            "Netflix Castlevania anime art style, Powerhouse Animation Studios quality, "
            "gothic horror aesthetic, medieval Wallachia setting, candlelit stone cathedrals, "
            "blood moon skies, baroque architecture, intricate Belmont whip detail, "
            "vampire lord regalia, alchemical glyphs, deep crimson and obsidian palette with gold accents, "
            "fluid hand-drawn combat animation feel, dynamic motion smears, dramatic chiaroscuro lighting, "
            "painterly backgrounds, 4K, cinematic dark fantasy composition"
        ),
        "vibe_hint": (
            "Gothic horror dark fantasy, Netflix Castlevania tone. Vampires, hunters, alchemists, "
            "speakers of the dead. Whip combat, magic glyphs, cathedrals, demon courts. "
            "Mature, witty, foul-mouthed banter under existential dread. Visceral fight choreography, "
            "blood is a design element. Romantic gothic intensity. Long brooding monologues offset by sharp gallows humor."
        ),
    },
}

DEFAULT_STYLE = "solo_leveling"


def get(style_key: str) -> dict:
    return STYLES.get(style_key, STYLES[DEFAULT_STYLE])


def list_public() -> list:
    """Trimmed list for the frontend picker — no internal hint text leaked."""
    return [
        {"key": k, "name": v["name"], "badge": v["badge"]}
        for k, v in STYLES.items()
    ]
