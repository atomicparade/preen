lint:
	black generate_gallery.py
	mypy generate_gallery.py
	pylint generate_gallery.py

clean_test:
	rm -rf build/test

debug: clean_test
	python generate_gallery.py -d test_gallery

test: clean_test
	python generate_gallery.py test_gallery
