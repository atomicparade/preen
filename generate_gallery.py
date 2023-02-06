# pylint: disable=missing-module-docstring

import logging
import hashlib
import html
import math
import re
import os
import shutil
import sys
import urllib.parse

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Union

import av  # type: ignore
import pyexiv2  # type: ignore
import tomli
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

DEFAULT_PERMISSIONS = 0o755

GALLERY_SETTINGS_FILENAME = "gallery.toml"

ALBUM_SETTINGS_FILENAME = "album.toml"

THUMBNAILS_DIR_NAME = "thumbnails"


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


def read_metadata(filename: Union[str, Path]) -> dict[str, str]:
    """Return a dict of EXIF, IPTC, and XMP extracted from a file."""
    file = pyexiv2.Image(f"{filename}")

    metadata = file.read_exif()
    metadata.update(file.read_iptc())
    metadata.update(file.read_xmp())

    file.close()

    return metadata


def strip_gps_data(filename: Union[str, Path]) -> None:
    """Remove GPS data from a file."""
    file = pyexiv2.Image(f"{filename}")

    def remove_gps_keys(metadata: dict):
        data_changed = False

        for key, _value in metadata.items():
            if "gps" in key.lower():
                metadata[key] = None
                data_changed = True

        return metadata, data_changed

    metadata = file.read_exif()
    metadata, data_changed = remove_gps_keys(metadata)
    if data_changed:
        file.modify_exif(metadata)

    metadata = file.read_iptc()
    metadata, data_changed = remove_gps_keys(metadata)
    if data_changed:
        file.modify_iptc(metadata)

    metadata = file.read_xmp()
    metadata, data_changed = remove_gps_keys(metadata)
    if data_changed:
        file.modify_xmp(metadata)

    file.close()

    return metadata


def is_sideways_orientation(orientation: Optional[int]) -> bool:
    """Return True if the orientation indicates rotation of 90 or 270 deg."""
    # 1 = Horizontal (normal)
    # 2 = Mirror horizontal
    # 3 = Rotate 180
    # 4 = Mirror vertical
    # 5 = Mirror horizontal and rotate 270 CW
    # 6 = Rotate 90 CW
    # 7 = Mirror horizontal and rotate 90 CW
    # 8 = Rotate 270 CW
    return orientation in [5, 6, 7, 8]


def orient_image(image: Image, orientation: Optional[int]) -> Image:
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


# def get_image_metadata(album_settings: AlbumSettings, file_path: Path) -> ImageMetadata:
#     """Retrieve the title, timestamp, location, and orientation of an image."""
#     metadata = read_metadata(file_path)

#     title = get_first_existing_attr(
#         metadata,
#         [
#             "Xmp.dc.title",
#             "Xmp.acdsee.caption",
#             "Iptc.Application2.ObjectName",
#         ],
#     )

#     timestamp = None
#     if album_settings.show_timestamps:
#         timestamp_str = get_first_existing_attr(
#             metadata,
#             [
#                 "Exif.Photo.DateTimeOriginal",
#                 "Exif.Image.DateTime",
#                 "Exif.Photo.DateTimeDigitized",
#             ],
#         )

#         if timestamp_str is not None:
#             # The date may be in the format YYYY:MM:DD
#             # If it is, change it to YYYY-MM-DD
#             if RE_DATE_HAS_COLONS.search(timestamp_str):
#                 timestamp_str = timestamp_str.replace(":", "-", 2)

#             if not RE_ENDS_WITH_OFFSET.search(timestamp_str):
#                 timestamp_str = f"{timestamp_str}{album_settings.default_time_offset}"

#             timestamp = datetime.fromisoformat(timestamp_str)
#         else:
#             # There was no EXIF DateTimeOriginal, so use the earlier of the file
#             # ctime and mtime
#             ctime = os.path.getctime(file_path)
#             mtime = os.path.getmtime(file_path)
#             timestamp = datetime.fromtimestamp(min(ctime, mtime))
#             timestamp = timestamp.replace(tzinfo=timezone.utc)

#     location = None
#     if not album_settings.strip_gps_data:
#         location = get_first_existing_attr(
#             metadata,
#             [
#                 "Exif.Image.ImageDescription",
#                 "Iptc.Application2.Caption",
#                 "Xmp.acdsee.notes",
#                 "Xmp.dc.description",
#                 "Xmp.exif.UserComment",
#                 "Xmp.tiff.ImageDescription",
#             ],
#         )

#         if (
#             location is None
#             and "Exif.GPSInfo.GPSLatitudeRef" in metadata
#             and "Exif.GPSInfo.GPSLatitude" in metadata
#             and "Exif.GPSInfo.GPSLongitudeRef" in metadata
#             and "Exif.GPSInfo.GPSLongitude" in metadata
#         ):
#             location = (
#                 f"{get_gps_dms_form(metadata['Exif.GPSInfo.GPSLatitude'])} "
#                 f"{metadata['Exif.GPSInfo.GPSLatitudeRef']} "
#                 f"{get_gps_dms_form(metadata['Exif.GPSInfo.GPSLongitude'])} "
#                 f"{metadata['Exif.GPSInfo.GPSLongitudeRef']}"
#             )

#     orientation = get_first_existing_attr(
#         metadata,
#         [
#             "Exif.Image.Orientation",
#         ],
#     )

#     if orientation is not None:
#         orientation = int(orientation)

#     return ImageMetadata(
#         title=title,
#         timestamp=timestamp,
#         location=location,
#         orientation=orientation,
#     )


# def get_video_metadata(album_settings: AlbumSettings, file_path: Path) -> VideoMetadata:
#     """Retrieve the title, timestamp, location, and orientation of a video."""
#     metadata = read_metadata(f"{file_path}.xmp")

#     title = get_first_existing_attr(
#         metadata,
#         [
#             "Iptc.Application2.ObjectName",
#             "Xmp.acdsee.caption",
#             "Xmp.dc.title",
#         ],
#     )

#     timestamp = None
#     if album_settings.show_timestamps:
#         timestamp_str = get_first_existing_attr(
#             metadata,
#             [
#                 "Exif.Photo.DateTimeOriginal",
#                 "Exif.Image.DateTime",
#                 "Exif.Photo.DateTimeDigitized",
#             ],
#         )

#         if timestamp_str is not None:
#             # The date may be in the format YYYY:MM:DD
#             # If it is, change it to YYYY-MM-DD
#             if RE_DATE_HAS_COLONS.search(timestamp_str):
#                 timestamp_str = timestamp_str.replace(":", "-", 2)

#             if not RE_ENDS_WITH_OFFSET.search(timestamp_str):
#                 timestamp_str = f"{timestamp_str}{album_settings.default_time_offset}"

#             timestamp = datetime.fromisoformat(timestamp_str)
#         else:
#             # There was no EXIF DateTimeOriginal, so use the earlier of the file
#             # ctime and mtime
#             ctime = os.path.getctime(file_path)
#             mtime = os.path.getmtime(file_path)
#             timestamp = datetime.fromtimestamp(min(ctime, mtime))
#             timestamp = timestamp.replace(tzinfo=timezone.utc)

#     location = None
#     if not album_settings.strip_gps_data:
#         location = get_first_existing_attr(
#             metadata,
#             [
#                 "Exif.Image.ImageDescription",
#                 "Iptc.Application2.Caption",
#                 "Xmp.acdsee.notes",
#                 "Xmp.dc.description",
#                 "Xmp.exif.UserComment",
#                 "Xmp.tiff.ImageDescription",
#             ],
#         )

#         if (
#             location is None
#             and "Exif.GPSInfo.GPSLatitudeRef" in metadata
#             and "Exif.GPSInfo.GPSLatitude" in metadata
#             and "Exif.GPSInfo.GPSLongitudeRef" in metadata
#             and "Exif.GPSInfo.GPSLongitude" in metadata
#         ):
#             location = (
#                 f"{get_gps_dms_form(metadata['Exif.GPSInfo.GPSLatitude'])} "
#                 f"{metadata['Exif.GPSInfo.GPSLatitudeRef']} "
#                 f"{get_gps_dms_form(metadata['Exif.GPSInfo.GPSLongitude'])} "
#                 f"{metadata['Exif.GPSInfo.GPSLongitudeRef']}"
#             )

#     width = None
#     if "Exif.Image.ImageWidth" in metadata:
#         width = int(metadata["Exif.Image.ImageWidth"])

#     height = None
#     if "Exif.Image.ImageHeight" in metadata:
#         width = int(metadata["Exif.Image.ImageHeight"])

#     orientation = get_first_existing_attr(
#         metadata,
#         [
#             "Exif.Image.Orientation",
#         ],
#     )

#     if orientation is not None:
#         orientation = int(orientation)

#     return VideoMetadata(
#         title=title,
#         timestamp=timestamp,
#         location=location,
#         width=width,
#         height=height,
#         orientation=orientation,
#     )


# def process_image_file(
#     album_settings: AlbumSettings, file_path: Path
# ) -> Optional[ImageFile]:
#     """Obtain image metadata, resize the image, and create a thumbnail."""
#     filename = os.path.basename(file_path)
#     logger.debug("Processing <%s>", filename)

#     try:
#         image = Image.open(file_path)
#     except UnidentifiedImageError:
#         logger.warning("    PIL was unable to read image at path <%s>", file_path)
#         return None

#     metadata = get_image_metadata(album_settings, file_path)

#     logger.debug("    Title: %s", metadata.title)
#     logger.debug("    Timestamp: %s", metadata.timestamp)
#     logger.debug("    Location: %s", metadata.location)
#     logger.debug(
#         "    Orientation: %s (%ssideways)",
#         metadata.orientation,
#         "" if is_sideways_orientation(metadata.orientation) else "not ",
#     )

#     # resize photos to maximum dimensions
#     max_width = album_settings.max_width
#     max_height = album_settings.max_height

#     if is_sideways_orientation(metadata.orientation):
#         max_width, max_height = max_height, max_width

#     image.thumbnail((max_width, max_height))

#     # save to album/
#     final_path = Path(os.path.join(album_settings.album_dir, filename))
#     image.save(final_path, exif=image.getexif())

#     if album_settings.strip_gps_data:
#         strip_gps_data(final_path)

#     # create thumbnail
#     image = orient_image(image, metadata.orientation)
#     image.thumbnail((album_settings.thumbnail_width, album_settings.thumbnail_height))

#     # centre the thumbnail in its container
#     thumbnail_position = (
#         math.floor((album_settings.thumbnail_width - image.width) / 2),
#         math.floor((album_settings.thumbnail_height - image.height) / 2),
#     )

#     thumbnail_image = Image.new(
#         "RGB",
#         (album_settings.thumbnail_width, album_settings.thumbnail_height),
#         (0, 0, 0),
#     )
#     thumbnail_image.paste(image, thumbnail_position)

#     # save thumbnail to album/thumbnails/
#     thumbnail_path = Path(os.path.join(album_settings.thumbnails_dir, filename))
#     thumbnail_image.save(thumbnail_path)

#     return ImageFile(
#         url=urllib.parse.quote(f"{filename}"),
#         thumbnail_url=urllib.parse.quote(f"thumbnails/{filename}"),
#         filename=filename,
#         metadata=metadata,
#     )


# def process_video_file(
#     album_settings: AlbumSettings, file_path: Path
# ) -> Optional[VideoFile]:
#     """Obtain video metadata, resize the video, and create a thumbnail."""
#     filename = os.path.basename(file_path)
#     logger.debug("Processing <%s>", filename)

#     try:
#         metadata = get_video_metadata(album_settings, file_path)
#     except RuntimeError as error:
#         logger.warning("    Error: %s", error)
#         return None

#     logger.debug("    Title: %s", metadata.title)
#     logger.debug("    Timestamp: %s", metadata.timestamp)
#     logger.debug("    Location: %s", metadata.location)
#     logger.debug(
#         "    Orientation: %s (%ssideways)",
#         metadata.orientation,
#         "" if is_sideways_orientation(metadata.orientation) else "not ",
#     )

#     final_path = Path(os.path.join(album_settings.album_dir, filename))

#     shutil.copy2(file_path, final_path)

#     # Create video thumbnail
#     container = av.open(str(file_path))
#     frames = container.decode(video=0)  # Get the first video stream
#     first_frame = next(frames)
#     image = first_frame.to_image()

#     image = orient_image(image, metadata.orientation)
#     image.thumbnail((album_settings.thumbnail_width, album_settings.thumbnail_height))

#     # centre the thumbnail in its container
#     thumbnail_position = (
#         math.floor((album_settings.thumbnail_width - image.width) / 2),
#         math.floor((album_settings.thumbnail_height - image.height) / 2),
#     )

#     thumbnail_image = Image.new(
#         "RGB",
#         (album_settings.thumbnail_width, album_settings.thumbnail_height),
#         (0, 0, 0),
#     )
#     thumbnail_image.paste(image, thumbnail_position)

#     # save thumbnail to album/thumbnails/
#     thumbnail_path = Path(
#         os.path.join(album_settings.thumbnails_dir, f"{filename}.jpg")
#     )
#     thumbnail_image.save(thumbnail_path)

#     return VideoFile(
#         url=urllib.parse.quote(f"{filename}"),
#         thumbnail_url=urllib.parse.quote(f"thumbnails/{filename}.jpg"),
#         filename=filename,
#         metadata=metadata,
#     )


# def generate_album(image_dir_name: str) -> None:
#     """Generate an HTML album for files in a directory."""
#     logger.debug("image_dir_name = %s", image_dir_name)

#     #  1) read album-settings.yaml file, if present
#     #      - album_title = {cwd basename}
#     #      - public_album = False - whether or not album is shown on gallery page
#     #      - max_width = None  - Files will be resized to fit the maximum
#     #      - max_height = None - width and height, if specified
#     #      - thumbnail_width = 100
#     #      - thumbnail_height = 100
#     #      - strip_gps_data = True - Whether or not to remove GPS data from images
#     #      - default_time_offset = "+00:00" - Default time offset, if not in metadata
#     #      - show_timestamps = True - Whether or not to show image/video timestamps
#     #      - sort_key = "timestamp" - Can be "filename" or "timestamp"

#     album_settings = AlbumSettings()

#     album_settings_file_path = os.path.join(image_dir_name, "album-settings.yaml")

#     try:
#         with open(
#             album_settings_file_path, "r", encoding="utf-8"
#         ) as album_settings_file:
#             settings = yaml.safe_load(album_settings_file)

#         if "album_title" in settings:
#             album_settings.album_title = settings["album_title"]
#         else:
#             album_settings.album_title = os.path.basename(image_dir_name)

#         for attr_name in [
#             "max_width",
#             "max_height",
#             "thumbnail_width",
#             "thumbnail_height",
#             "strip_gps_data",
#             "default_time_offset",
#             "show_timestamps",
#             "sort_key",
#         ]:
#             if attr_name in settings:
#                 setattr(album_settings, attr_name, settings[attr_name])
#     except FileNotFoundError:
#         album_settings.album_title = os.path.basename(image_dir_name)

#     logger.debug("%s", album_settings)

#     # 2) create album/ and album/thumbnails/
#     album_dir_name = os.path.join(image_dir_name, "album")
#     thumbnails_dir_name = os.path.join(album_dir_name, "thumbnails")

#     album_dir = Path(album_dir_name)
#     thumbnails_dir = Path(thumbnails_dir_name)

#     album_dir.mkdir(mode=0o755, exist_ok=True)
#     thumbnails_dir.mkdir(mode=0o755, exist_ok=True)

#     album_settings.album_dir = album_dir
#     album_settings.thumbnails_dir = thumbnails_dir

#     # 3) find and process all file
#     file_paths = list(Path(image_dir_name).glob("*"))

#     files: List[Union[ImageFile, VideoFile]] = []

#     for file_path in file_paths:
#         # Ignore files that don't appear to be images
#         if not is_image_file(file_path) and not is_video_file(file_path):
#             continue

#         file: Optional[Union[ImageFile, VideoFile]]

#         if is_image_file(file_path):
#             file = process_image_file(album_settings, file_path)
#         elif is_video_file(file_path):
#             file = process_video_file(album_settings, file_path)
#         else:
#             continue

#         if file:
#             files.append(file)

#     # 4) sort photos
#     if album_settings.sort_key == "timestamp":
#         files.sort(
#             key=lambda file: file.metadata.timestamp or datetime.now(timezone.utc)
#         )
#     elif album_settings.sort_key == "filename":
#         files.sort(key=lambda file: file.filename)

#     # 5) create album index.html
#     album_file_path = Path(os.path.join(album_dir, "index.html"))
#     write_album_file(album_settings, files, album_file_path)


# pylint: disable=too-many-instance-attributes
class PageSettings:
    """Stores settings for gallery and album generation."""

    title: Optional[str] = None
    output_directory_name: Optional[str] = None
    is_public: bool = False
    strip_gps_data: bool = True
    max_image_width: Optional[int] = None
    max_image_height: Optional[int] = None
    thumbnail_width: int = 100
    thumbnail_height: int = 100
    default_time_offset: str = "+00:00"
    show_timestamps: bool = True
    sort_key: str = "timestamp"
    foreground_color: str = "#eeeeee"  # TODO: Use this value for the gallery
    background_color: str = "#333333"  # TODO: Use this value for the gallery
    link_color: str = "#44aadd"  # TODO: Use this value for the gallery

    def clone(self):
        """Create a copy of the settings, except for the title and output directory name."""

        copy = PageSettings()

        for attr in dir(self):
            if not (
                attr.startswith("__")
                or callable(getattr(self, attr))
                or (attr in ["title", "output_directory_name"])
            ):
                setattr(copy, attr, getattr(self, attr))

        return copy

    def debug_print(self):
        """Log the settings."""

        for attr in dir(self):
            if not (attr.startswith("__") or callable(getattr(self, attr))):
                logger.debug("%24s = %s", attr, getattr(self, attr))


class ImageFile:
    """Store info for a file needed to generate the HTML album."""

    path: Path

    url: str
    thumbnail_url: str

    title: Optional[str]
    timestamp: Optional[datetime]
    location: Optional[str]  # Description, caption, or GPS coordinates
    orientation: Optional[int]

    def __init__(self, path: Path):
        self.path = path

    def process(
        self, settings: PageSettings, output_path: Path, thumbnails_path: Path
    ) -> None:
        """Resize image if necessary, generate thumbnail, and copy to output dir."""

        self.url = (
            self.path.name
        )  # TODO: handle filename change for MP$-converted files
        self.thumbnail_url = f"{THUMBNAILS_DIR_NAME}/{self.url}"

    def get_thumbnail_html(self) -> str:
        """Generate HTML snippet for the image's thumbnail."""

        #     if metadata.title is not None:
        #         alt_tag = f'alt="{html.escape(metadata.title)}" '
        #     else:
        #         alt_tag = f'alt="{html.escape(file.filename)}" '

        #     img_tag = (
        #         f'<img src="{file.thumbnail_url}" '
        #         f"{alt_tag}"
        #         f'width="{album_settings.thumbnail_width}" '
        #         f'height="{album_settings.thumbnail_height}">'
        #     )

        #     return f'<a href="#file-{idx}">{img_tag}</a>'
        return "TODO"

    def get_html(self) -> str:
        """Generate HTML snippet for the image."""

        #     if metadata.title is not None:
        #         alt_tag = f'alt="{html.escape(metadata.title)}" '
        #     else:
        #         alt_tag = f'alt="{html.escape(file.filename)}" '

        #     img_tag = (
        #         f'<img src="{file.thumbnail_url}" '
        #         f"{alt_tag}"
        #         f'width="{album_settings.thumbnail_width}" '
        #         f'height="{album_settings.thumbnail_height}">'
        #     )

        # if metadata.location is not None:
        #     content_parts.append(
        #         '<a href="https://duckduckgo.com/?iaxm=maps&q='
        #         f"{urllib.parse.quote(metadata.location)}"
        #         f'">🗺️ {html.escape(metadata.location)}</a>'
        #     )

        #     return f'<a href="#file-{idx}">{img_tag}</a>'
        return "TODO"


class VideoFile:
    """Store info for a file needed to generate the HTML album."""

    path: Path

    url: str
    thumbnail_url: str

    title: Optional[str]
    timestamp: Optional[datetime]
    location: Optional[str]  # Description, caption, or GPS coordinates
    width: Optional[int]
    height: Optional[int]
    orientation: Optional[int]

    def __init__(self, path: Path):
        self.path = path

    def process(
        self, settings: PageSettings, output_path: Path, thumbnails_path: Path
    ) -> None:
        """Re-encode video if necessary, generate thumbnail, and copy to output dir."""

        self.url = (
            self.path.name
        )  # TODO: handle filename change for MP$-converted files
        self.thumbnail_url = f"{THUMBNAILS_DIR_NAME}/{self.url}"

    def get_thumbnail_html(self) -> str:
        """Generate HTML snippet for the video's thumbnail."""

        #     if metadata.title is not None:
        #         alt_tag = f'alt="{html.escape(metadata.title)}" '
        #     else:
        #         alt_tag = f'alt="{html.escape(file.filename)}" '

        #     img_tag = (
        #         f'<img src="{file.thumbnail_url}" '
        #         f"{alt_tag}"
        #         f'width="{album_settings.thumbnail_width}" '
        #         f'height="{album_settings.thumbnail_height}">'
        #     )

        #     return f'<a href="#file-{idx}" class="video-thumbnail">{img_tag}</a>'
        return "TODO"

    def get_html(self) -> str:
        """Generate HTML snippet for the video."""

        # content_parts.append(f'<video controls><source src="{file.url}"></video>')

        # if metadata.title is not None:
        #     content_parts.append(html.escape(metadata.title))

        # if metadata.timestamp is not None:
        #     content_parts.append(
        #         f'<time datetime="{metadata.timestamp}">'
        #         f'{metadata.timestamp.strftime("%-d %B %Y")}'
        #         f"</time>"
        #     )

        # if metadata.location is not None:
        #     content_parts.append(
        #         '<a href="https://duckduckgo.com/?iaxm=maps&q='
        #         f"{urllib.parse.quote(metadata.location)}"
        #         f'">🗺️ {html.escape(metadata.location)}</a>'
        #     )

        # return "<br>".join(content_parts)
        return "TODO"


class Album:
    """Looks for images in a directory and generates an album page for them."""

    path: Path
    output_base_path: Path
    output_path: Path
    thumbnails_path: Path

    # Values are inherited from the gallery's page settings if not specified
    settings: PageSettings

    # TODO: If file has no datetime, DO NOT USE FILE CREATION TIME - don't use any time at all

    def __init__(self, path: Path, output_base_path: Path):
        self.path = path
        self.output_base_path = output_base_path

    def generate(self, default_settings: PageSettings):
        """Start the process of album generation."""
        logger.debug("Generating album for <%s>", self.path)

        self.read_settings(default_settings)
        self.create_album()

    def read_settings(self, default_settings: PageSettings):
        """Retrieve the settings for the album."""
        self.settings = default_settings.clone()

        settings_path = self.path.joinpath(ALBUM_SETTINGS_FILENAME)

        if not os.path.exists(settings_path):
            raise RuntimeError(f"Unable to find settings file at <{settings_path}>")

        with open(settings_path, "rb") as settings_file:
            try:
                settings = tomli.load(settings_file)
            except tomli.TOMLDecodeError as err:
                raise RuntimeError(f"Unable to read album settings: {err}") from err

        for attr in dir(self.settings):
            if attr.startswith("__") or attr in ["path", "output_path"]:
                continue

            if attr in settings:
                setattr(self.settings, attr, settings[attr])

        if self.settings.output_directory_name is None:
            name_hash = hashlib.sha256(self.path.name.encode())
            self.settings.output_directory_name = name_hash.hexdigest()

        self.output_path = self.output_base_path.joinpath(
            self.settings.output_directory_name
        )

        self.thumbnails_path = self.output_path.joinpath(THUMBNAILS_DIR_NAME)

        self.settings.debug_print()

        logger.debug("Output path: %s", self.output_path)

    def create_album(self):
        """Process the image and video files."""
        os.makedirs(self.thumbnails_path, mode=DEFAULT_PERMISSIONS, exist_ok=True)

        files = []

        for file_path in Path(self.path).glob("*"):
            if is_image_file(file_path):
                image_file = ImageFile(file_path)
                image_file.process(
                    self.settings, self.output_path, self.thumbnails_path
                )
                files.append(image_file)
            elif is_video_file(file_path):
                video_file = VideoFile(file_path)
                video_file.process(
                    self.settings, self.output_path, self.thumbnails_path
                )
                files.append(video_file)

        self.write_album_index(files)

    def write_album_index(self, files: List[Union[ImageFile, VideoFile]]):
        """Generate an HTML file for an album."""
        index_file_path = self.output_path.joinpath("index.html")

        with open(index_file_path, "w", encoding="utf-8") as index_file:
            index_file.write(
                f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{self.settings.title}</title>
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <meta property="og:title" content="{self.settings.title}">
  <meta name="twitter:title" content="{self.settings.title}">
  <style>
body {{
    color: {self.settings.foreground_color};
    background: {self.settings.background_color};
    font-family: sans-serif;
}}

@media screen {{
    a {{
        color: {self.settings.link_color}
    }}

    p.return-to-gallery {{
        text-align: left;
    }}
}}
"""
            )

            index_file.write(
                """\
html {
    scroll-behavior: smooth;
}

@media print {
    nav {
        display: none;
    }

    .file img, .file video {
        max-width: 100%;
    }

    p {
        margin: 0 0 2em;
    }

    p.return-to-gallery {
        display: none;
    }
}

@media
    screen and (max-width: 768px),

    /* Tablets and smartphones */
    screen and (hover: none)
{
    body {
        margin: 1em;
        padding: 0;
    }

    h1 {
        margin-bottom: 0.7rem;
    }

    nav {
        display: none;
    }

    .file img, .file video {
        max-width: 100%;
        max-height: calc(100vh - 4.5em);
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
        margin: 0;
        padding: 0;
    }

    h1 {
        background: inherit;
        position: fixed;
        margin: 0;
        padding: 2rem 4rem 1rem;
        top: 0;
        left: 0;
        height: 2rem;
        width: calc(100% - 10rem);
    }

    p.return-to-gallery {
        background: inherit;
        position: fixed;
        margin: 0;
        padding: 0 4rem;
        top: 5rem;
        height: 2rem;
        width: calc(100% - 10rem);
    }

    nav {
        top: 7rem;
        left: 4rem;
        max-height: calc(100% - 5.6rem);
        width: calc(40% - 4rem);
        display: flex;
        flex-direction: row;
        flex-wrap: wrap;
        align-items: flex-start;
        margin: -0.4rem -0.4rem 0;
        overflow: auto;
        position: fixed;
    }

    #photos {
        margin-top: 6em;
        margin-left: calc(40% + 1em);
        width: calc(60% - 5em);
    }

    nav a {
        margin: 0.4em;
        text-decoration: none;
        position: relative;
    }

    nav a.video-thumbnail:before {
        color: #f8f8f8;
        background: #00000099;
        content: "▶";
        position: absolute;
        top: calc(50% - 0.7em - 0.05em);
        left: calc(50% - 0.5em - 0.3em);
        font-size: 1.5em;
        padding: 0.05em 0.2em 0.05em 0.4em;
        width: 1em;
        height: 1.4em;
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
  <h1>{self.settings.title}</h1>
  <p class="return-to-gallery"><a href="..">Return to gallery</a></p>
  <div id="album">
    <nav>
"""
            )

            # TODO: Put placeholder here if there are no files

            # TODO: # write thumbnails
            # for file, idx in zip(files, range(1, len(files) + 1)):
            #     if isinstance(file, ImageFile):
            #         thumbnail_html = get_image_thumbnail_html(album_settings, file, idx)
            #     elif isinstance(file, VideoFile):
            #         thumbnail_html = get_video_thumbnail_html(album_settings, file, idx)

            #     index_file.write(f"{thumbnail_html}\n")

            index_file.write(
                """\
    </nav>
    <div id="photos">
"""
            )

            # TODO: # write files
            # for file, idx in zip(files, range(1, len(files) + 1)):
            #     if isinstance(file, ImageFile):
            #         file_html = get_image_html(file, idx)
            #     elif isinstance(file, VideoFile):
            #         file_html = get_video_html(file)

            #     index_file.write(f'<p id="file-{idx}" class="file">{file_html}</p>\n')

            index_file.write(
                """\
    </div>
  </div>
</body>
</html>
"""
            )


class Gallery:
    """Looks for albums in subdirectories and generates a gallery page for them."""

    path: Path
    output_path: Path

    settings: PageSettings = PageSettings()

    def __init__(self, path: Path):
        self.path = path

    def generate(self) -> None:
        """Start the process of album generation."""
        logger.debug("Generating gallery for <%s>", self.path)

        self.read_settings()
        self.create_gallery()

    def read_settings(self):
        """Retrieve the settings for the gallery."""
        settings_path = self.path.joinpath(GALLERY_SETTINGS_FILENAME)

        if not os.path.exists(settings_path):
            raise RuntimeError(f"Unable to find settings file at <{settings_path}>")

        with open(settings_path, "rb") as settings_file:
            try:
                settings = tomli.load(settings_file)
            except tomli.TOMLDecodeError as err:
                raise RuntimeError(f"Unable to read gallery settings: {err}") from err

        for attr in dir(self.settings):
            if attr.startswith("__") or attr in ["path", "output_path"]:
                continue

            if attr in settings:
                setattr(self.settings, attr, settings[attr])

        if self.settings.output_directory_name is None:
            self.settings.output_directory_name = "gallery"

        self.output_path = self.path.joinpath(self.settings.output_directory_name)

        self.settings.debug_print()

        logger.debug("Output path: %s", self.output_path)

    def create_gallery(self):
        """Find the album subdirectories and process them."""
        os.makedirs(self.output_path, mode=DEFAULT_PERMISSIONS, exist_ok=True)

        albums = []

        for album_path in Path(self.path).glob("*"):
            album_settings_path = album_path.joinpath("album.toml")

            if album_settings_path.exists():
                try:
                    album = Album(album_path, self.output_path)
                    album.generate(self.settings)

                    if album.settings.is_public:
                        albums.append(album)
                except RuntimeError as err:
                    logger.error("Unable to generate album: %s", err)

        # TODO: Sort the albums

        if len(albums) == 0:
            pass
            # TODO: Then there are no public albums

        # TODO: Generate index.html


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
    dir_names = []

    for arg in sys.argv[1:]:
        if arg in ["-d", "--debug"]:
            logger.setLevel(logging.DEBUG)
        elif arg in ["-h", "--help"]:
            # TODO: Print help information
            print("TODO: Print help information")
        else:
            dir_names.append(arg)

    if len(dir_names) == 0:
        dir_names.append(os.getcwd())

    for dir_name in dir_names:
        try:
            gallery = Gallery(Path(dir_name))
            gallery.generate()
        except RuntimeError as err:
            logger.error(err)


if __name__ == "__main__":
    main()
