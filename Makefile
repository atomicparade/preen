lint:
	black generate_album.py
	mypy generate_album.py
	pylint generate_album.py
