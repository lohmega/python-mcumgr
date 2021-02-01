.PHONY: clean 

clean:
	rm -fr build/
	rm -fr dist/
	rm -fr .eggs/
	find . -name '*.egg-info' -exec rm -fr {} +
	find . -name '*.egg' -exec rm -f {} +
	
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +

# hack - Using pip to install is more permisive then 
# 	python setup.py install. i.e. works without sudo
install: clean 
	python3 setup.py sdist bdist_wheel
	python3 -m pip install dist/lohmega-python-mcumgr-*.tar.gz

