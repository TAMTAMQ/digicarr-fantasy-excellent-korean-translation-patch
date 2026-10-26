"""Deterministic typography for the black bg100 ending card."""
from PIL import Image, ImageDraw, ImageFont


def render_end_card(font_path):
    # Match the original 640x480 card's lower-right text area. This asset has
    # no illustration: author it from text so it remains reproducible in Git.
    image = Image.new("RGBA", (640, 480), (0, 0, 0, 255))
    font = ImageFont.truetype(str(font_path), 44)
    ImageDraw.Draw(image).text((580, 438), "끝", font=font, fill=(215, 215, 215), anchor="rs")
    return image
