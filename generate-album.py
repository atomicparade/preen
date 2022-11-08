import logging
import math
import re
import os
import sys
import urllib

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import exifread  # type: ignore
import yaml  # type: ignore

from PIL import Image, UnidentifiedImageError  # type: ignore


logger = logging.getLogger(__name__)

RE_IMAGE_EXTENSION = re.compile(
    "^.+\\.(bmp|gif|jfif|jpe?g|png|tiff?|tga|webp)$", re.IGNORECASE
)


def get_images(image_directory, thumbnail_directory, thumbnail_size):
    thumbnail_directory = Path(thumbnail_directory)

    for file in [file for file in thumbnail_directory.glob("*")]:
        file.unlink()

    thumbnail_directory.mkdir(mode=0o755, exist_ok=True)

    files = [file for file in Path(image_directory).glob("*")]

    images = []

    for file in files:
        thumbnail_name = Path(thumbnail_directory, file.stem + ".jpg")

        image = Image.open(file)
        image.thumbnail(thumbnail_size)

        top_left = (0, 0)

        if image.width < thumbnail_size[0]:
            top_left = (
                math.floor(abs(image.width - thumbnail_size[0]) / 2),
                top_left[1],
            )

        if image.height < thumbnail_size[1]:
            top_left = (
                top_left[0],
                math.floor(abs(image.height - thumbnail_size[1]) / 2),
            )

        final_image = Image.new("RGB", thumbnail_size, (0, 0, 0))
        final_image.paste(image, top_left)
        final_image.save(thumbnail_name, "jpeg")

        if "_" in file.stem:
            description = file.stem.split("_", maxsplit=1)[1]
        else:
            description = file.stem

        images.append(
            {
                "path": str(file),
                "thumbnail": thumbnail_name,
                "description": description,
                "stem": file.stem,
            }
        )

    def get_image_file_number(image):
        if re.match(r"^(\d+)", image["stem"]) is not None:
            return int(re.split(r"^(\d+)", image["stem"])[1])
        else:
            return 999

    images = sorted(images, key=get_image_file_number)

    return images


def write_html(file, images, page_title, thumbnail_size):
    file.write(
        f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{page_title}</title>
  <link rel="stylesheet" type="text/css" href="album.css">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
</head>
<body>
  <h1>{page_title}</h1>
  <div id="album">
    \
"""
    )

    # write thumbnails
    for image, idx in zip(images, range(1, len(images) + 1)):
        thumbnail_path = urllib.parse.quote(str(image["thumbnail"]).replace("\\", "/"))

        file.write(
            f"""\
<p id="thumbnail-{idx}" class="thumbnail"><img src="{thumbnail_path}" alt="{image['description']}" width="{thumbnail_size[0]}" height="{thumbnail_size[1]}"></p>\
"""
        )

    file.write(
        f"""\

    <div id="large-view">
      <p id="instructions" class="image">Hover over an image</p>
"""
    )

    # write images
    for image, idx in zip(images, range(1, len(images) + 1)):
        image_path = urllib.parse.quote(str(image["path"]).replace("\\", "/"))

        file.write(
            f"""\
      <p id="image-{idx}" class="image"><img src="{image_path}" alt="{image['description']}"><br>{image['description']}</p>
"""
        )

    file.write(
        f"""\
    </div>
  </div>
</body>
</html>
"""
    )


def write_css(file, images):
    file.write(
        """\
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

    if len(images) > 0:
        for idx in range(1, len(images) + 1):
            file.write(
                f"""\
    #thumbnail-{idx}:hover ~ #large-view #image-{idx}\
"""
            )

            if idx < len(images):
                file.write(
                    """\
,
"""
                )

        file.write(
            """\
 {
        display: block;
    }
"""
        )

    file.write(
        """\
}
"""
    )


@dataclass
class AlbumSettings:
    gallery_title: str = ""
    maximum_width: Optional[int] = None
    maximum_height: Optional[int] = None
    thumbnail_width: int = 100
    thumbnail_height: int = 100
    strip_gps_data: bool = True


@dataclass
class ImageFile:
    path: Path
    filename: str
    image: Image
    timestamp: datetime
    caption: Optional[str]


def generate_album(image_dir_name: str) -> None:
    logger.debug("image_dir_name = %s", image_dir_name)

    #  1) read album-settings.yaml file, if present
    #      - gallery_title = {cwd basename}
    #      - maximum_width = None  - Images will be resized to fit the maximum
    #      - maximum_height = None - width and height, if specified
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
            "maximum_width",
            "maximum_height",
            "thumbnail_width",
            "thumbnail_height",
            "strip_gps_data",
        ]:
            if attr_name in settings:
                setattr(album_settings, attr_name, settings[attr_name])
    except FileNotFoundError:
        album_settings.gallery_title = os.path.basename(image_dir_name)

    logger.debug("%s", album_settings)

    #  2) create gallery/ and gallery/thumbnails/
    gallery_dir_name = os.path.join(image_dir_name, "gallery")
    thumbnails_dir_name = os.path.join(gallery_dir_name, "thumbnails")

    gallery_dir = Path(gallery_dir_name)
    thumbnails_dir = Path(thumbnails_dir_name)

    gallery_dir.mkdir(mode=0o755, exist_ok=True)
    thumbnails_dir.mkdir(mode=0o755, exist_ok=True)

    #  3) create gallery/index.html with page header and gallery title

    #  4) find all photos
    file_paths = list(Path(image_dir_name).glob("*"))

    files = []

    for file_path in file_paths:
        # Ignore files that don't appear to be images
        if not RE_IMAGE_EXTENSION.match(str(file_path)):
            continue

        filename = os.path.basename(file_path)

        try:
            image = Image.open(file_path)
        except UnidentifiedImageError:
            logger.warning("PIL was unable to read image at path <%s>", file_path)
            continue

        try:
            with open(file_path, "rb") as image_file:
                tags = exifread.process_file(image_file)

            caption = tags.get("EXIF UserComment", None)

            if not caption:
                caption = tags.get("Image ImageDescription", None)

            date_time_original = str(tags["EXIF DateTimeOriginal"])

            # There is no time offset specified, so just fall back to UTC
            # The original datetime, even with the wrong time zone, will
            # hopefully be closer to the actual original date and time than
            # the file ctime or mtime
            offset_time_original = tags.get("EXIF OffsetTimeOriginal", "+00:00")

            timestamp_str = (
                f"{date_time_original[0:4]}-"
                f"{date_time_original[5:7]}-"
                f"{date_time_original[8:]} "
                f"{offset_time_original}"
            )

            timestamp = datetime.fromisoformat(timestamp_str)
            logger.debug("<%s> EXIF original date: %s", filename, timestamp)
        except (KeyError, ValueError, IndexError):
            # There was no EXIF DateTimeOriginal, so use the earlier of the file
            # ctime and mtime
            ctime = os.path.getctime(file_path)
            mtime = os.path.getmtime(file_path)
            timestamp = datetime.fromtimestamp(min(ctime, mtime))
            timestamp = timestamp.replace(tzinfo=timezone.utc)
            logger.debug("<%s> file date: %s", filename, timestamp)

        files.append(
            ImageFile(
                path=file_path,
                filename=filename,
                image=Image,
                timestamp=timestamp,
                caption=caption,
            )
        )

    #  5) sort photos by EXIF taken date (or by file date, if EXIF date not present)
    files.sort(key=lambda file: file.timestamp)

    for file in files:
        logger.debug(f"{file.timestamp} \"{file.caption or ''}\" <{file.filename}>")

    #  6) resize photos to maximum dimensions
    #  7) save each file to gallery/ as: {yyyy}{mm}{dd}{hh}{mm}{ss}_{alphanumeric_caption}
    #  8) resize to thumbnail size
    #  9) save each thumbnail to gallery/thumbnails/
    # 10) add photo to index.html
    # 11) add page footer


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
