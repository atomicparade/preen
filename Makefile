lint:
	black generate_gallery.py
	mypy generate_gallery.py
	pylint generate_gallery.py
