# pylint: disable=missing-module-docstring

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
from typing import List, Optional, Tuple, Union

import yaml  # type: ignore

from PIL import Image, UnidentifiedImageError  # type: ignore


logger = logging.getLogger(__name__)

RE_IMAGE_EXTENSION = re.compile(
    r"\.(bmp|gif|jfif|jpeg|jpg|png|tif|tiff|tga|webp)$", re.IGNORECASE
)

RE_VIDEO_EXTENSION = re.compile(
    r"\.(3gp|avi|m4v|mp4|mkv|mov|mpeg|mpg|webm|wmv)$", re.IGNORECASE
)

RE_DATE_HAS_COLONS = re.compile(r"^\d{4}:\d{2}:\d{2}")

RE_ENDS_WITH_OFFSET = re.compile(r"(-|\+)\d{2}:\d{2}$")


def is_image_file(file_path: Path) -> bool:
    """Returns True if the path ends with an image extension."""
    return RE_IMAGE_EXTENSION.search(str(file_path)) is not None


def is_video_file(file_path: Path) -> bool:
    """Returns True if the path ends with a video extension."""
    return RE_VIDEO_EXTENSION.search(str(file_path)) is not None


def get_image_tags(filename: Union[str, Path]) -> dict[str, str]:
    """Run exiftool on the provided file and returns the results as a dict."""
    results = subprocess.check_output(
        ["exiftool", f"{str(filename)}"], encoding="utf-8"
    )

    metadata = {}

    for line in results.split("\n"):
        parts = line.split(":", 1)

        if len(parts) == 2:
            metadata[parts[0].strip()] = parts[1].strip()

    return metadata


def strip_gps_data(filename: Union[str, Path]) -> None:
    """Run exiftool to strip GPS data from the specified file."""
    subprocess.check_output(
        ["exiftool", "-gps*=", "-overwrite_original", f"{str(filename)}"]
    )


def is_sideways_orientation(orientation: Optional[str]) -> bool:
    """Return True if the orientation indicates rotation of 90 or 270 deg."""
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
    """Rotate/flip the image according to the EXIF rotation string."""
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


# TODO: Add sorting option (sort by date, filename)
# pylint: disable=too-many-instance-attributes
@dataclass
class AlbumSettings:
    """Store the settings for a photo album."""

    gallery_title: str = ""
    max_width: Optional[int] = None
    max_height: Optional[int] = None
    thumbnail_width: int = 100
    thumbnail_height: int = 100
    strip_gps_data: bool = True
    default_time_offset: str = "+00:00"
    gallery_dir: Path = Path()
    thumbnails_dir: Path = Path()
    show_timestamps: bool = True
    sort_key: str = "timestamp"


@dataclass
class FileMetadata:
    """Store metadata for a file extracted from EXIF/IPTC/XMP or file system."""

    title: Optional[str]
    timestamp: Optional[datetime]
    location: Optional[str]
    orientation: Optional[str]


@dataclass
class AlbumFile:
    """Store info for a file needed to generate the HTML album."""

    url: str
    thumbnail_url: str
    filename: str
    metadata: FileMetadata


def fit_to_dimensions(
    width: float, height: float, max_width: Optional[int], max_height: Optional[int]
) -> Tuple[int, int]:
    """Fit a rectangle to the given max width and height."""
    aspect_ratio = width / height

    if max_width is not None and width > max_width:
        width = max_width
        height = width / aspect_ratio

    if max_height is not None and height > max_height:
        height = max_height
        width = height * aspect_ratio

    width = math.floor(width)
    height = math.floor(height)

    return (width, height)


def get_image_metadata(album_settings: AlbumSettings, file_path: Path) -> FileMetadata:
    """Retrieve the title, timestamp, location, and orientation of an image."""
    metadata = get_image_tags(file_path)

    title = None
    for attr_name in ["Title", "Object Name"]:
        if attr_name in metadata:
            title = metadata[attr_name].strip()
            break

    timestamp = None
    if album_settings.show_timestamps:
        if "Date/Time Original" in metadata:
            timestamp_str = metadata["Date/Time Original"]

            # The date may be in the format YYYY:MM:DD
            # If it is, change it to YYYY-MM-DD
            if RE_DATE_HAS_COLONS.search(timestamp_str):
                timestamp_str = timestamp_str.replace(":", "-", 2)

            if not RE_ENDS_WITH_OFFSET.search(timestamp_str):
                timestamp_str = f"{timestamp_str}{album_settings.default_time_offset}"

            timestamp = datetime.fromisoformat(timestamp_str)
        else:
            # There was no EXIF DateTimeOriginal, so use the earlier of the file
            # ctime and mtime
            ctime = os.path.getctime(file_path)
            mtime = os.path.getmtime(file_path)
            timestamp = datetime.fromtimestamp(min(ctime, mtime))
            timestamp = timestamp.replace(tzinfo=timezone.utc)

    location = None
    if not album_settings.strip_gps_data:
        for attr_name in [
            "Image Description",
            "User Comment",
            "Notes",
            "Description",
            "Caption-Abstract",
        ]:
            if attr_name in metadata:
                location = metadata[attr_name].strip()
                break

        if location is None:
            if "GPS Latitude" in metadata and "GPS Longitude" in metadata:
                location = (
                    f"{metadata['GPS Latitude']}, {metadata['GPS Longitude']}".replace(
                        " deg", "°"
                    )
                )

    orientation = metadata.get("Orientation", None)

    return FileMetadata(
        title=title,
        timestamp=timestamp,
        location=location,
        orientation=orientation,
    )


def get_video_metadata(_file_path: Path) -> FileMetadata:
    """Retrieve the title, timestamp, location, and orientation of a video."""
    # TODO: Implement get_video_metadata
    return FileMetadata(
        title="TODO",
        timestamp=None,
        location=None,
        orientation=None,
    )


def process_image_file(
    album_settings: AlbumSettings, file_path: Path
) -> Optional[AlbumFile]:
    """Obtain image metadata, resize the image, and create a thumbnail."""
    filename = os.path.basename(file_path)
    logger.debug("Processing <%s>", filename)

    try:
        image = Image.open(file_path)
    except UnidentifiedImageError:
        logger.warning("    PIL was unable to read image at path <%s>", file_path)
        return None

    metadata = get_image_metadata(album_settings, file_path)

    logger.debug("    Title: %s", metadata.title)
    logger.debug("    Timestamp: %s", metadata.timestamp)
    logger.debug("    Location: %s", metadata.location)
    logger.debug(
        "    Orientation: %s (%ssideways)",
        metadata.orientation,
        "" if is_sideways_orientation(metadata.orientation) else "not ",
    )

    # resize photos to maximum dimensions
    max_width = album_settings.max_width
    max_height = album_settings.max_height

    if is_sideways_orientation(metadata.orientation):
        max_width, max_height = max_height, max_width

    (final_width, final_height) = fit_to_dimensions(
        image.width, image.height, max_width, max_height
    )

    if final_height != image.height:
        image = image.resize((final_width, final_height))

    # save to gallery/
    final_path = Path(os.path.join(album_settings.gallery_dir, filename))
    image.save(final_path, exif=image.getexif())

    if album_settings.strip_gps_data:
        strip_gps_data(final_path)

    # create thumbnail
    image = orient_image(image, metadata.orientation)

    (thumbnail_width, thumbnail_height) = fit_to_dimensions(
        image.width,
        image.height,
        album_settings.thumbnail_width,
        album_settings.thumbnail_height,
    )

    image = image.resize((thumbnail_width, thumbnail_height))

    # centre the thumbnail in its container
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
    thumbnail_path = Path(os.path.join(album_settings.thumbnails_dir, filename))
    thumbnail_image.save(thumbnail_path)

    return AlbumFile(
        url=urllib.parse.quote(f"{filename}"),
        thumbnail_url=urllib.parse.quote(f"thumbnails/{filename}"),
        filename=filename,
        metadata=metadata,
    )


def process_video_file(
    _album_settings: AlbumSettings, _file_path: Path
) -> Optional[AlbumFile]:
    """Obtain video metadata, resize the video, and create a thumbnail."""
    # TODO: process_video_file
    return None


def write_gallery_file(
    album_settings: AlbumSettings, files: List[AlbumFile], path: Path
) -> None:
    """Generate an HTML file for a gallery."""
    with open(path, "w", encoding="utf-8") as index_file:
        # write page header
        index_file.write(
            f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{album_settings.gallery_title}</title>
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <meta property="og:title" content="{album_settings.gallery_title}">
  <meta name="twitter:title" content="{album_settings.gallery_title}">
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

    .file img {
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

    .file:nth-child(2) img {
        margin-top: 0;
    }

    .file img {
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

    .file {
        background: #333;
        display: none;
        position: fixed;
        top: 2em;
        left: 40%;
        text-align: center;
        height: 90vh;
        width: calc(60% - 4em);
    }

    .file img {
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
    #thumbnail-{idx}:hover ~ #large-view #file-{idx}\
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
    \
"""
        )

        # write thumbnails
        for file, idx in zip(files, range(1, len(files) + 1)):
            metadata = file.metadata

            if metadata.title is None:
                alt_tag = ""
            else:
                alt_tag = f'alt="{html.escape(metadata.title)}" '

            img_tag = (
                f'<img src="{file.thumbnail_url}" '
                f"{alt_tag}"
                f'width="{album_settings.thumbnail_width}" '
                f'height="{album_settings.thumbnail_height}">'
            )

            index_file.write(
                f"""\
<p id="thumbnail-{idx}" class="thumbnail">{img_tag}</p>\
"""
            )

        index_file.write(
            """\

    <div id="large-view">
      <p id="instructions" class="file">Hover over an image</p>
"""
        )

        # write files
        for file, idx in zip(files, range(1, len(files) + 1)):
            metadata = file.metadata

            html_title = html.escape(str(metadata.title))

            if metadata.title is None:
                alt_tag = ""
            else:
                alt_tag = f'alt="{html_title}" '

            img_tag = f'<img src="{file.url}"{alt_tag}>'

            caption_parts: List[str] = []

            if metadata.timestamp is not None:
                time_tag = (
                    f'<time datetime="{metadata.timestamp}">'
                    f'{metadata.timestamp.strftime("%d %B %Y")}'
                    f"</time>"
                )

            if metadata.title is not None and metadata.timestamp is not None:
                caption_parts.append(f"{html_title} - {time_tag}")
            elif metadata.title is not None:
                caption_parts.append(html_title)
            elif metadata.timestamp is not None:
                caption_parts.append(time_tag)

            if metadata.location is not None:
                location_tag = (
                    '<a href="https://duckduckgo.com/?iaxm=maps&q='
                    f"{urllib.parse.quote(metadata.location)}"
                    f'">🗺️ {html.escape(metadata.location)}</a>'
                )

                caption_parts.append(location_tag)

            caption = "<br>".join(caption_parts)

            index_file.write(
                f"""\
      <p id="file-{idx}" class="file">{img_tag}<br>{caption}</p>
"""
            )

        # write page footer
        index_file.write(
            """\
    </div>
  </div>
</body>
</html>
"""
        )


def generate_album(image_dir_name: str) -> None:
    """Generate an HTML album for files in a directory."""
    logger.debug("image_dir_name = %s", image_dir_name)

    #  1) read album-settings.yaml file, if present
    #      - gallery_title = {cwd basename}
    #      - max_width = None  - Files will be resized to fit the maximum
    #      - max_height = None - width and height, if specified
    #      - thumbnail_width = 100
    #      - thumbnail_height = 100
    #      - strip_gps_data = True
    album_settings = AlbumSettings()

    album_settings_file_path = os.path.join(image_dir_name, "album-settings.yaml")

    try:
        with open(
            album_settings_file_path, "r", encoding="utf-8"
        ) as album_settings_file:
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
            "show_timestamps",
            "sort_key",
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

    album_settings.gallery_dir = gallery_dir
    album_settings.thumbnails_dir = thumbnails_dir

    # 3) find and process all file
    file_paths = list(Path(image_dir_name).glob("*"))

    files = []

    for file_path in file_paths:
        # Ignore files that don't appear to be images
        if not is_image_file(file_path) and not is_video_file(file_path):
            continue

        if is_image_file(file_path):
            file = process_image_file(album_settings, file_path)
        elif is_video_file(file_path):
            file = process_video_file(album_settings, file_path)
        else:
            continue

        if file:
            files.append(file)

    # 4) sort photos
    if album_settings.sort_key == "timestamp":
        files.sort(key=lambda file: file.metadata.timestamp or datetime.now())
    elif album_settings.sort_key == "filename":
        files.sort(key=lambda file: file.filename)

    # 5) create gallery index.html
    gallery_file_path = Path(os.path.join(gallery_dir, "index.html"))
    write_gallery_file(album_settings, files, gallery_file_path)


def main() -> None:
    """Called when the program is invoked."""
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
        # TODO: Add an option that causes the program to iterate through every
        # directory in the specified directory
        if arg in ["-d", "--debug"]:
            logger.setLevel(logging.DEBUG)
        elif arg in ["-h", "--help"]:
            # TODO: Print help information
            print("TODO: Print help information")
        else:
            image_dir_names.append(arg)

    if len(image_dir_names) == 0:
        image_dir_names.append(os.getcwd())

    # TODO: If iterating through every directory, create a landing page that
    # links to each gallery
    # However, the galleries should be stored in directories at the same level
    # of the directory containing the landing page, NOT in a subdirectory of the
    # landing page
    # By default, ignore directories that begin with .

    # TODO: Support video files
    for image_dir_name in image_dir_names:
        generate_album(image_dir_name.rstrip("/\\"))


if __name__ == "__main__":
    main()
