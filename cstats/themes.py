"""Custom Textual themes for cstats.

Registers two extra themes on top of Textual's 21 builtins:
  - orange: warm orange on dark background — nearly every accent is an orange
    shade, modeled after the orstats "orange" palette
  - nexat:  NEXAT brand colours from the official styleguide (Aug 2024):
      NEXred #B41918 (Pantone 2350c / RAL 3020), NEXAT Black #000000,
      White #FFFFFF, plus RAL 7035 light grey and RAL 7016 anthracite

Cycle themes in the TUI with `t`; selection is persisted via config.py.
"""

from textual.theme import Theme

# orstats orange palette: accent #ff8c00, ok #ffa500, info #ffcc66,
# catalogue #ff6600, warn #ffd700, danger #d70000 — orange everywhere.
ORANGE = Theme(
    name="orange",
    primary="#ff8c00",
    secondary="#ff6600",
    accent="#ffa500",
    foreground="#f0e2cc",
    background="#160f08",
    surface="#241708",
    panel="#2e1d0c",
    boost="#3d2712",
    warning="#ffd700",
    error="#d70000",
    success="#ffa500",
    dark=True,
    luminosity_spread=0.16,
    text_alpha=0.95,
    variables={
        "input-cursor-foreground": "#160f08",
        "footer-background": "#2e1d0c",
        "footer-key-foreground": "#ffcc66",
    },
)

# NEXAT styleguide colours (Styleguide_DE_ENG_240801, p.10):
#   NEXred   #B41918  RGB 180/25/24  CMYK 0/86/87/29  Pantone 2350c  RAL 3020
#   Black    #000000  CMYK 0/0/0/100
#   White    #FFFFFF
#   RAL 7035 light grey   ≈ #CBD2D9
#   RAL 7016 anthracite   ≈ #383E42
# For dark-terminal legibility the red accent is brightened (#e02a2a);
# the brand red itself stays the primary.
NEXAT = Theme(
    name="nexat",
    primary="#b41918",
    secondary="#383e42",
    accent="#e02a2a",
    foreground="#f2f4f6",
    background="#0b0c0d",
    surface="#1a1d1f",
    panel="#232729",
    boost="#2f3437",
    warning="#cbd2d9",
    error="#e02a2a",
    success="#cbd2d9",
    dark=True,
    luminosity_spread=0.14,
    text_alpha=0.96,
    variables={
        "input-cursor-foreground": "#0b0c0d",
        "footer-background": "#1a1d1f",
        "footer-key-foreground": "#e02a2a",
    },
)

CUSTOM_THEMES = [ORANGE, NEXAT]

# ordered list used by the `t` cycle key: custom themes first, then a curated
# subset of the builtins so cycling doesn't take 21 keypresses
CYCLE = [
    "orange",
    "nexat",
    "textual-dark",
    "textual-light",
    "nord",
    "gruvbox",
    "catppuccin-mocha",
    "tokyo-night",
    "dracula",
    "solarized-dark",
    "monokai",
]


def register_custom_themes(app) -> None:
    for theme in CUSTOM_THEMES:
        app.register_theme(theme)
