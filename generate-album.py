import logging
import html
import math
import re
import os
import subprocess
import sys
import urllib.parse

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import yaml  # type: ignore

from PIL import Image, UnidentifiedImageError  # type: ignore


logger = logging.getLogger(__name__)

RE_IMAGE_EXTENSION = re.compile(
    r"\.(bmp|gif|jfif|jpe?g|png|tiff?|tga|webp)$", re.IGNORECASE
)

RE_DATE_HAS_COLONS = re.compile(r"^\d{4}:\d{2}:\d{2}")

RE_ENDS_WITH_OFFSET = re.compile(r"(-|\+)\d{2}:\d{2}$")


def get_exif_data(filename: Union[str, Path]) -> dict[str, str]:
    """Runs exiftool on the provided file and returns the results as a dict."""
    results = subprocess.check_output(
        ["exiftool", f"{str(filename)}"], encoding="utf-8"
    )

    exif_data = {}

    for line in results.split("\n"):
        parts = line.split(":", 1)

        if len(parts) == 2:
            exif_data[parts[0].strip()] = parts[1].strip()

    return exif_data


def is_sideways_orientation(orientation: Optional[str]) -> bool:
    """Returns True if the orientation indicates rotation of 90 or 270 deg."""
    # https://exiftool.org/TagNames/EXIF.html
    # 1 = Horizontal (normal)
    # 2 = Mirror horizontal
    # 3 = Rotate 180
    # 4 = Mirror vertical
    # 5 = Mirror horizontal and rotate 270 CW
    # 6 = Rotate 90 CW
    # 7 = Mirror horizontal and rotate 90 CW
    # 8 = Rotate 270 CW
    return orientation in (
        "Mirror horizontal and rotate 270 CW",
        "Rotate 90 CW",
        "Mirror horizontal and rotate 90 CW",
        "Rotate 270 CW",
    )


def orient_image(image: Image, orientation: Optional[str]) -> Image:
    """Rotates/flips the image according to the EXIF rotation string."""
    if orientation in (
        "Mirror horizontal",
        "Mirror horizontal and rotate 270 CW",
        "Mirror horizontal and rotate 90 CW",
    ):
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

    if orientation in ("Mirror vertical",):
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    if orientation in (
        "Rotate 270 CW",
        "Mirror horizontal and rotate 270 CW",
    ):
        image = image.transpose(Image.Transpose.ROTATE_90)

    if orientation in ("Rotate 180",):
        image = image.transpose(Image.Transpose.ROTATE_180)

    if orientation in (
        "Rotate 90 CW",
        "Mirror horizontal and rotate 90 CW",
    ):
        image = image.transpose(Image.Transpose.ROTATE_270)

    return image


@dataclass
class AlbumSettings:
    """Stores the settings for a photo album."""

    gallery_title: str = ""
    max_width: Optional[int] = None
    max_height: Optional[int] = None
    thumbnail_width: int = 100
    thumbnail_height: int = 100
    strip_gps_data: bool = True
    default_time_offset: str = "+00:00"


@dataclass
class ImageFile:
    """Stores info for an image file needed to generate the HTML album."""

    image_url: str
    thumbnail_url: str
    filename: str
    timestamp: datetime
    caption: str


def generate_album(image_dir_name: str) -> None:
    logger.debug("image_dir_name = %s", image_dir_name)

    #  1) read album-settings.yaml file, if present
    #      - gallery_title = {cwd basename}
    #      - max_width = None  - Images will be resized to fit the maximum
    #      - max_height = None - width and height, if specified
    #      - thumbnail_width = 100
    #      - thumbnail_height = 100
    #      - strip_gps_data = True
    album_settings = AlbumSettings()

    album_settings_file_path = os.path.join(image_dir_name, "album-settings.yaml")

    try:
        with open(album_settings_file_path, "r") as album_settings_file:
            settings = yaml.safe_load(album_settings_file)

        if "gallery_title" in settings:
            album_settings.gallery_title = settings["gallery_title"]
        else:
            album_settings.gallery_title = os.path.basename(image_dir_name)

        for attr_name in [
            "max_width",
            "max_height",
            "thumbnail_width",
            "thumbnail_height",
            "strip_gps_data",
            "default_time_offset",
        ]:
            if attr_name in settings:
                setattr(album_settings, attr_name, settings[attr_name])
    except FileNotFoundError:
        album_settings.gallery_title = os.path.basename(image_dir_name)

    logger.debug("%s", album_settings)

    # 2) create gallery/ and gallery/thumbnails/
    gallery_dir_name = os.path.join(image_dir_name, "gallery")
    thumbnails_dir_name = os.path.join(gallery_dir_name, "thumbnails")

    gallery_dir = Path(gallery_dir_name)
    thumbnails_dir = Path(thumbnails_dir_name)

    gallery_dir.mkdir(mode=0o755, exist_ok=True)
    thumbnails_dir.mkdir(mode=0o755, exist_ok=True)

    # 3) find and process all photos
    file_paths = list(Path(image_dir_name).glob("*"))

    files = []

    for file_path in file_paths:
        # Ignore files that don't appear to be images
        if not RE_IMAGE_EXTENSION.search(str(file_path)):
            continue

        filename = os.path.basename(file_path)
        logger.debug("Processing <%s>", filename)

        try:
            image = Image.open(file_path)
        except UnidentifiedImageError:
            logger.warning("PIL was unable to read image at path <%s>", file_path)
            continue

        exif_data = get_exif_data(file_path)

        title = exif_data.get("Title", "")
        if title == "":
            title = exif_data.get("Object Name", "")

        title = ""
        for attr_name in ["Title", "Object Name"]:
            if attr_name in exif_data:
                title = exif_data[attr_name]
                break

        caption = ""
        for attr_name in [
            "Image Description",
            "User Comment",
            "Notes",
            "Description",
            "Caption-Abstract",
        ]:
            if attr_name in exif_data:
                caption = exif_data[attr_name]
                break

        orientation = exif_data.get("Orientation", None)

        logger.debug("    Title: %s", title)
        logger.debug("    Caption: %s", caption)
        logger.debug(
            "    Orientation: %s (%ssideways)",
            str(orientation),
            "" if is_sideways_orientation(orientation) else "not ",
        )

        if "Date/Time Original" in exif_data:
            timestamp_str = exif_data["Date/Time Original"]

            # The date may be in the format YYYY:MM:DD
            # If it is, change it to YYYY-MM-DD
            if RE_DATE_HAS_COLONS.search(timestamp_str):
                timestamp_str = timestamp_str.replace(":", "-", 2)

            if not RE_ENDS_WITH_OFFSET.search(timestamp_str):
                timestamp_str = f"{timestamp_str}{album_settings.default_time_offset}"

            timestamp = datetime.fromisoformat(timestamp_str)
            logger.debug("    EXIF original date: %s", timestamp)
        else:
            # There was no EXIF DateTimeOriginal, so use the earlier of the file
            # ctime and mtime
            ctime = os.path.getctime(file_path)
            mtime = os.path.getmtime(file_path)
            timestamp = datetime.fromtimestamp(min(ctime, mtime))
            timestamp = timestamp.replace(tzinfo=timezone.utc)
            logger.debug("    File date: %s", timestamp)

        # resize photos to maximum dimensions
        max_width = album_settings.max_width
        max_height = album_settings.max_height

        final_width = image.width
        final_height = image.height
        ar = image.width / image.height

        if is_sideways_orientation(orientation):
            max_width, max_height = max_height, max_width

        if max_width is not None:
            if final_width > max_width:
                final_width = max_width
                final_height = final_width / ar

        if max_height is not None:
            if final_height > max_height:
                final_height = max_height
                final_width = final_height * ar

        if final_height != image.height:
            final_width = math.floor(final_width)
            final_height = math.floor(final_height)
            image = image.resize((final_width, final_height))

        # save to gallery/
        final_path = Path(os.path.join(gallery_dir, filename))
        image.save(final_path, exif=image.getexif())

        # create thumbnail
        image = orient_image(image, orientation)

        thumbnail_width = album_settings.thumbnail_width
        thumbnail_height = thumbnail_width / ar

        if thumbnail_height > album_settings.thumbnail_height:
            thumbnail_height = album_settings.thumbnail_height
            thumbnail_width = thumbnail_height * ar

        thumbnail_width = math.floor(thumbnail_width)
        thumbnail_height = math.floor(thumbnail_height)

        if is_sideways_orientation(orientation):
            thumbnail_width, thumbnail_height = thumbnail_height, thumbnail_width

        image = image.resize((thumbnail_width, thumbnail_height))

        thumbnail_position = (
            math.floor((album_settings.thumbnail_width - thumbnail_width) / 2),
            math.floor((album_settings.thumbnail_height - thumbnail_height) / 2),
        )

        thumbnail_image = Image.new(
            "RGB",
            (album_settings.thumbnail_width, album_settings.thumbnail_height),
            (0, 0, 0),
        )
        thumbnail_image.paste(image, thumbnail_position)

        # save thumbnail to gallery/thumbnails/
        thumbnail_path = Path(os.path.join(thumbnails_dir, filename))
        thumbnail_image.save(thumbnail_path)

        image_url = urllib.parse.quote(f"{filename}")
        thumbnail_url = urllib.parse.quote(f"thumbnails/{filename}")

        # TODO: strip GPS data if indicated

        # TODO: if not stripping GPS data, add hyperlink to map provider with
        # GPS coordinates

        # TODO: the image probably doesn't need to be saved in ImageFile

        files.append(
            ImageFile(
                image_url=image_url,
                thumbnail_url=thumbnail_url,
                filename=filename,
                timestamp=timestamp,
                caption=caption,
            )
        )

    # 4) sort photos by EXIF taken date (or by file date, if EXIF date not present)
    files.sort(key=lambda file: file.timestamp)

    # TODO: Create OpenGraph image and description

    # 5) create gallery index.html
    index_file_path = os.path.join(gallery_dir, "index.html")
    with open(index_file_path, "w") as index_file:
        # write page header
        index_file.write(
            f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{album_settings.gallery_title}</title>
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
"""
        )

        index_file.write(
            """\
  <style>
@media print {
    body {
        font-family: sans-serif;
    }

    .thumbnail {
        display: none;
    }

    #instructions {
        display: none;
    }

    .image img {
        max-width: 100%;
        margin-bottom: 1em;
    }
}

@media
    screen and (max-width: 768px),

    /* Tablets and smartphones */
    screen and (hover: none)
{
    body {
        background: #333;
        color: #eee;
        font-family: sans-serif;
        margin: 1em;
        padding: 0;
    }

    h1 {
        margin-top: 0;
    }

    .thumbnail {
        display: none;
    }

    #instructions {
        display: none;
    }

    .image:nth-child(2) img {
        margin-top: 0;
    }

    .image img {
        max-width: calc(100vw - 3em);
    }
}

@media
    screen and (min-width: 769px) and (hover: hover),

    /* IE10 and IE11 (they don't support (hover: hover) */
    screen and (min-width: 769px) and (-ms-high-contrast: none),
    screen and (min-width: 769px) and (-ms-high-contrast: active)
{
    body {
        background: #333;
        color: #eee;
        font-family: sans-serif;
        margin: 2em 60% 2em 4em;
        padding: 0;
    }

    .album {
        display: flex;
        flex-direction: row;
        flex-wrap: wrap;
    }

    .thumbnail {
        display: inline-block;;
        margin: 0 .5em .2em 0;
    }

    .image {
        background: #333;
        display: none;
        position: fixed;
        top: 2em;
        left: 40%;
        text-align: center;
        height: 90vh;
        width: calc(60% - 4em);
    }

    .image img {
        display: block;
        max-height: 92%;
        max-width: 100%;
        margin: 0 auto;
    }

    #instructions {
        display: block;
        top: 4em;
    }

"""
        )

        for idx in range(1, len(files) + 1):
            index_file.write(
                f"""\
    #thumbnail-{idx}:hover ~ #large-view #image-{idx}\
"""
            )

            if idx < len(files):
                index_file.write(
                    """\
,
"""
                )

        index_file.write(
            """\
 {
        display: block;
    }
}
  </style>
"""
        )

        index_file.write(
            f"""\
</head>
<body>
  <h1>{album_settings.gallery_title}</h1>
  <div id="album">
"""
        )

        # write thumbnails
        for image, idx in zip(files, range(1, len(files) + 1)):
            index_file.write(
                f"""\
    <p id="thumbnail-{idx}" class="thumbnail"><img src="{image.thumbnail_url}" alt="{html.escape(image.caption)}" width="{album_settings.thumbnail_width}" height="{album_settings.thumbnail_height}"></p>
"""
            )

        index_file.write(
            f"""\
    <div id="large-view">
      <p id="instructions" class="image">Hover over an image</p>
"""
        )

        # write images
        for image, idx in zip(files, range(1, len(files) + 1)):
            index_file.write(
                f"""\
      <p id="image-{idx}" class="image"><img src="{image.image_url}" alt="{html.escape(image.caption)}"><br><time datetime=\"{image.timestamp}\">{image.timestamp.strftime("%Y-%m-%d")}</time> - {html.escape(image.caption)}</p>
"""
            )

        # write page footer
        index_file.write(
            f"""\
    </div>
  </div>
</body>
</html>
"""
        )


def main():
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "[{asctime}] [{levelname:<8}] {name}: {message}",
            "%Y-%m-%d %H:%M:%S",
            style="{",
        )
    )
    logger.addHandler(handler)

    # If any directories were specified, use them;
    # otherwise, default to current working directory
    image_dir_names = []

    for arg in sys.argv[1:]:
        if arg in ["-d", "--debug"]:
            logger.setLevel(logging.DEBUG)
        elif arg in ["-h", "--help"]:
            print("TODO: Print help information")
        else:
            image_dir_names.append(arg)

    if len(image_dir_names) == 0:
        image_dir_names.append(os.getcwd())

    for image_dir_name in image_dir_names:
        generate_album(image_dir_name.rstrip("/\\"))


if __name__ == "__main__":
    main()
