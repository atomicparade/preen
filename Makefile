lint:
	black generate-album.py
	mypy generate-album.py
	pylint generate-album.py
