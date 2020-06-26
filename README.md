# photo-album

[![Unlicense](https://img.shields.io/badge/license-Unlicense-blue)](https://choosealicense.com/licenses/unlicense/) [![CC0](https://img.shields.io/badge/license-CC0-blue)](https://creativecommons.org/publicdomain/zero/1.0/)

Generate a minimal photo album.

## How to use

1. Copy `config-EXAMPLE` to `config` in the directory where you want to generate the album.
2. Edit the configuration as desired.
3. `pip install -r requirements.txt` (optionally, create a virtual environment)
4. `python generate-album.py`

### Captioning photos

Files are ordered by filenames and captions are extracted from filenames as follows:

1. Files are sorted by natural order (1.png < 2.png < 10.png < a.png).
2. If an underscore is present in the filename, only the portion after the first underscore, without the extension, is used as the caption. Otherwise, the entire filename without the extension is used as the caption.

## Example

![Example photo album](docs/example.png)
