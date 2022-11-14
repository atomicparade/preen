# pylint: disable=missing-module-docstring

import logging
import html
import math
import re
import os
import shutil
import subprocess
import sys
import urllib.parse

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

import pyexiv2  # type: ignore
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


def parse_gps_part(part: str) -> float:
    """Parse a GPS coordinate component into a number (e.g., 1200/100 = 1.2)."""
    subparts = part.split("/", 2)

    if len(subparts) == 1:
        return float(subparts[0])

    numerator = int(subparts[0])
    denominator = int(subparts[1])
    return numerator / denominator


def get_gps_dms_form(coordinate: str) -> str:
    """Convert a GPS coordinate into degrees, minutes, and seconds."""
    parts = coordinate.split()

    hours = parse_gps_part(parts[0])
    minutes = parse_gps_part(parts[1])
    seconds = parse_gps_part(parts[2])

    decimal_degrees = hours + minutes / 60 + seconds / 3600

    hours = int(decimal_degrees)

    decimal_degrees = (decimal_degrees - hours) * 60
    minutes = int(decimal_degrees)

    decimal_degrees = (decimal_degrees - minutes) * 60
    seconds = decimal_degrees

    return f"{hours}° {minutes}' {seconds:.2f}\""


def get_first_existing_attr(obj: dict, attr_names: List[str]) -> Any:
    """Return the first attribute that exists in obj, or None."""
    for attr_name in attr_names:
        if attr_name in obj:
            if isinstance(obj[attr_name], dict):
                return list(obj[attr_name].values())[0]
            return obj[attr_name]

    return None


def is_image_file(file_path: Path) -> bool:
    """Returns True if the path ends with an image extension."""
    return RE_IMAGE_EXTENSION.search(str(file_path)) is not None


def is_video_file(file_path: Path) -> bool:
    """Returns True if the path ends with a video extension."""
    return RE_VIDEO_EXTENSION.search(str(file_path)) is not None


def get_image_data(filename: Union[str, Path]) -> dict[str, str]:
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


def read_metadata(filename: Union[str, Path]) -> dict[str, str]:
    """Return a dict of EXIF, IPTC, and XMP extracted from a file."""
    xmp_file = pyexiv2.Image(f"{filename}")

    metadata = {}

    metadata = xmp_file.read_exif()
    metadata.update(xmp_file.read_iptc())
    metadata.update(xmp_file.read_xmp())

    xmp_file.close()

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
    return orientation in [5, 6, 7, 8]


def orient_image(image: Image, orientation: Optional[str]) -> Image:
    """Rotate/flip the image according to the EXIF rotation string."""
    if orientation in [2, 5, 7]:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

    if orientation in [4]:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    if orientation in [5, 8]:
        image = image.transpose(Image.Transpose.ROTATE_90)

    if orientation in [3]:
        image = image.transpose(Image.Transpose.ROTATE_180)

    if orientation in [6, 7]:
        image = image.transpose(Image.Transpose.ROTATE_270)

    return image


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
class ImageMetadata:
    """Store metadata for an image extracted from EXIF/IPTC/XMP or file system."""

    title: Optional[str]
    timestamp: Optional[datetime]
    location: Optional[str]
    orientation: Optional[str]


@dataclass
class VideoMetadata:
    """Store metadata for a video extracted from EXIF/IPTC/XMP or file system."""

    title: Optional[str]
    timestamp: Optional[datetime]
    location: Optional[str]
    width: Optional[int]
    height: Optional[int]


@dataclass
class ImageFile:
    """Store info for a file needed to generate the HTML album."""

    url: str
    thumbnail_url: str
    filename: str
    metadata: ImageMetadata


@dataclass
class VideoFile:
    """Store info for a file needed to generate the HTML album."""

    url: str
    thumbnail_url: str
    filename: str
    metadata: VideoMetadata


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


def get_image_metadata(album_settings: AlbumSettings, file_path: Path) -> ImageMetadata:
    """Retrieve the title, timestamp, location, and orientation of an image."""
    metadata = read_metadata(file_path)

    title = get_first_existing_attr(
        metadata,
        [
            "Xmp.dc.title",
            "Xmp.acdsee.caption",
            "Iptc.Application2.ObjectName",
        ],
    )

    timestamp = None
    if album_settings.show_timestamps:
        timestamp_str = get_first_existing_attr(
            metadata,
            [
                "Exif.Photo.DateTimeOriginal",
                "Exif.Image.DateTime",
                "Exif.Photo.DateTimeDigitized",
            ],
        )

        if timestamp_str is not None:
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
        location = get_first_existing_attr(
            metadata,
            [
                "Exif.Image.ImageDescription",
                "Iptc.Application2.Caption",
                "Xmp.acdsee.notes",
                "Xmp.dc.description",
                "Xmp.exif.UserComment",
                "Xmp.tiff.ImageDescription",
            ],
        )

        if (
            location is None
            and "Exif.GPSInfo.GPSLatitudeRef" in metadata
            and "Exif.GPSInfo.GPSLatitude" in metadata
            and "Exif.GPSInfo.GPSLongitudeRef" in metadata
            and "Exif.GPSInfo.GPSLongitude" in metadata
        ):
            location = (
                f"{get_gps_dms_form(metadata['Exif.GPSInfo.GPSLatitude'])} "
                f"{metadata['Exif.GPSInfo.GPSLatitudeRef']} "
                f"{get_gps_dms_form(metadata['Exif.GPSInfo.GPSLongitude'])} "
                f"{metadata['Exif.GPSInfo.GPSLongitudeRef']}"
            )

    orientation = get_first_existing_attr(
        metadata,
        [
            "Exif.Image.Orientation",
        ],
    )

    if orientation is not None:
        orientation = int(orientation)

    return ImageMetadata(
        title=title,
        timestamp=timestamp,
        location=location,
        orientation=orientation,
    )


def get_video_metadata(album_settings: AlbumSettings, file_path: Path) -> VideoMetadata:
    """Retrieve the title, timestamp, location, and orientation of a video."""
    metadata = read_metadata(f"{file_path}.xmp")

    title = get_first_existing_attr(
        metadata,
        [
            "Iptc.Application2.ObjectName",
            "Xmp.acdsee.caption",
            "Xmp.dc.title",
        ],
    )

    timestamp = None
    if album_settings.show_timestamps:
        timestamp_str = get_first_existing_attr(
            metadata,
            [
                "Exif.Photo.DateTimeOriginal",
                "Exif.Image.DateTime",
                "Exif.Photo.DateTimeDigitized",
            ],
        )

        if timestamp_str is not None:
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
        location = get_first_existing_attr(
            metadata,
            [
                "Exif.Image.ImageDescription",
                "Iptc.Application2.Caption",
                "Xmp.acdsee.notes",
                "Xmp.dc.description",
                "Xmp.exif.UserComment",
                "Xmp.tiff.ImageDescription",
            ],
        )

        if (
            location is None
            and "Exif.GPSInfo.GPSLatitudeRef" in metadata
            and "Exif.GPSInfo.GPSLatitude" in metadata
            and "Exif.GPSInfo.GPSLongitudeRef" in metadata
            and "Exif.GPSInfo.GPSLongitude" in metadata
        ):
            location = (
                f"{get_gps_dms_form(metadata['Exif.GPSInfo.GPSLatitude'])} "
                f"{metadata['Exif.GPSInfo.GPSLatitudeRef']} "
                f"{get_gps_dms_form(metadata['Exif.GPSInfo.GPSLongitude'])} "
                f"{metadata['Exif.GPSInfo.GPSLongitudeRef']}"
            )

    width = None
    if "Exif.Image.ImageWidth" in metadata:
        width = int(metadata["Exif.Image.ImageWidth"])

    height = None
    if "Exif.Image.ImageHeight" in metadata:
        width = int(metadata["Exif.Image.ImageHeight"])

    return VideoMetadata(
        title=title,
        timestamp=timestamp,
        location=location,
        width=width,
        height=height,
    )


def process_image_file(
    album_settings: AlbumSettings, file_path: Path
) -> Optional[ImageFile]:
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

    return ImageFile(
        url=urllib.parse.quote(f"{filename}"),
        thumbnail_url=urllib.parse.quote(f"thumbnails/{filename}"),
        filename=filename,
        metadata=metadata,
    )


def process_video_file(
    album_settings: AlbumSettings, file_path: Path
) -> Optional[VideoFile]:
    """Obtain video metadata, resize the video, and create a thumbnail."""
    filename = os.path.basename(file_path)
    logger.debug("Processing <%s>", filename)

    try:
        metadata = get_video_metadata(album_settings, file_path)
    except RuntimeError as error:
        logger.warning("    Error: %s", error)
        return None

    logger.debug("    Title: %s", metadata.title)
    logger.debug("    Timestamp: %s", metadata.timestamp)
    logger.debug("    Location: %s", metadata.location)

    final_path = Path(os.path.join(album_settings.gallery_dir, filename))

    shutil.copy2(file_path, final_path)

    # TODO: Create video thumbnail
    # TODO: Srip GPS data from video if option is indicated

    return VideoFile(
        url=urllib.parse.quote(f"{filename}"),
        thumbnail_url=urllib.parse.quote(f"thumbnails/{filename}.jpg"),
        filename=filename,
        metadata=metadata,
    )


def get_image_html(file: ImageFile, idx: int) -> str:
    """Get the HTML tags for an image file."""
    content_parts: List[str] = []
    metadata = file.metadata

    html_title = html.escape(str(metadata.title))

    if metadata.title is not None:
        alt_tag = f' alt="{html_title}" '
    else:
        alt_tag = f' alt="{html.escape(file.filename)}" '

    content_parts.append(f'<a href="#file-{idx}"><img src="{file.url}"{alt_tag}></a>')

    if metadata.title is not None:
        content_parts.append(html_title)

    if metadata.timestamp is not None:
        content_parts.append(
            f'<time datetime="{metadata.timestamp}">'
            f'{metadata.timestamp.strftime("%-d %B %Y")}'
            f"</time>"
        )

    if metadata.location is not None:
        content_parts.append(
            '<a href="https://duckduckgo.com/?iaxm=maps&q='
            f"{urllib.parse.quote(metadata.location)}"
            f'">🗺️ {html.escape(metadata.location)}</a>'
        )

    return "<br>".join(content_parts)


def get_video_html(file: VideoFile) -> str:
    """Get the HTML tags for a video file."""
    content_parts: List[str] = []
    metadata = file.metadata

    content_parts.append(f'<video controls><source src="{file.url}"></video>')

    if metadata.title is not None:
        content_parts.append(html.escape(metadata.title))

    if metadata.timestamp is not None:
        content_parts.append(
            f'<time datetime="{metadata.timestamp}">'
            f'{metadata.timestamp.strftime("%-d %B %Y")}'
            f"</time>"
        )

    if metadata.location is not None:
        content_parts.append(
            '<a href="https://duckduckgo.com/?iaxm=maps&q='
            f"{urllib.parse.quote(metadata.location)}"
            f'">🗺️ {html.escape(metadata.location)}</a>'
        )

    return "<br>".join(content_parts)


def write_gallery_file(
    album_settings: AlbumSettings, files: List[Union[ImageFile, VideoFile]], path: Path
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
html {
    scroll-behavior: smooth;
}

@media print {
    body {
        font-family: sans-serif;
    }

    nav {
        display: none;
    }

    .file img, .file video {
        max-width: 100%;
    }

    p {
        margin: 0 0 2em;
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

    nav {
        display: none;
    }

    .file img, .file video {
        max-width: 100%;
    }

    p {
        text-align: center;
        margin: 0 0 2em;
    }
}

@media
    screen and (min-width: 768px) and (hover: hover),

    /* IE10 and IE11 (they don't support (hover: hover) */
    screen and (min-width: 768px) and (-ms-high-contrast: none),
    screen and (min-width: 768px) and (-ms-high-contrast: active)
{
    body {
        background: #333;
        color: #eee;
        font-family: sans-serif;
        margin: 0;
        padding: 0;
    }

    a {
        color: #4ad;
    }

    h1 {
        background: inherit;
        position: fixed;
        margin: 0;
        padding: 2rem 4rem;
        top: 0;
        left: 0;
        height: 2rem;
        width: calc(100% - 8rem);
    }

    nav {
        top: 6em;
        left: 4em;
        max-height: calc(100% - 5.6em);
        width: calc(40% - 4em);
        display: flex;
        flex-direction: row;
        flex-wrap: wrap;
        align-items: flex-start;
        margin: -0.4em -0.4em 0;
        overflow: scroll;
        position: fixed;
    }

    #photos {
        margin-top: 6em;
        margin-left: calc(40% + 1em);
        width: calc(60% - 5em);
    }

    nav img {
        margin: 0.4em;
    }

    p {
        text-align: center;
        margin: -6em 0 2em;
        padding-top: 6em;
    }

    .file img, .file video {
        max-width: 100%;
        max-height: calc(100vh - 10.5em);
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
    <nav>
"""
        )

        # write thumbnails
        for file, idx in zip(files, range(1, len(files) + 1)):
            metadata = file.metadata

            if metadata.title is not None:
                alt_tag = f'alt="{html.escape(metadata.title)}" '
            else:
                alt_tag = f'alt="{html.escape(album_settings.gallery_title)}" '

            img_tag = (
                f'<img src="{file.thumbnail_url}" '
                f"{alt_tag}"
                f'width="{album_settings.thumbnail_width}" '
                f'height="{album_settings.thumbnail_height}">'
            )

            index_file.write(
                f"""\
<a href="#file-{idx}">{img_tag}</a>
"""
            )

        index_file.write(
            """\
    </nav>
    <div id="photos">
"""
        )

        # write files
        for file, idx in zip(files, range(1, len(files) + 1)):
            if isinstance(file, ImageFile):
                file_html = get_image_html(file, idx)
            elif isinstance(file, VideoFile):
                file_html = get_video_html(file)

            index_file.write(
                f"""\
      <p id="file-{idx}" class="file">{file_html}</p>
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

    files: List[Union[ImageFile, VideoFile]] = []

    for file_path in file_paths:
        # Ignore files that don't appear to be images
        if not is_image_file(file_path) and not is_video_file(file_path):
            continue

        file: Optional[Union[ImageFile, VideoFile]]

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
        files.sort(
            key=lambda file: file.metadata.timestamp or datetime.now(timezone.utc)
        )
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

    for image_dir_name in image_dir_names:
        generate_album(image_dir_name.rstrip("/\\"))


if __name__ == "__main__":
    main()
