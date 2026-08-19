import setuptools
import os

THIS_DIR = os.path.abspath(os.path.dirname(__file__))

VERSION = None
with open(os.path.join(THIS_DIR, "mcumgr", "__version__.py")) as f:
    tmp_dict = {}
    exec(f.read(), tmp_dict)
    VERSION = tmp_dict["__version__"]

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="lohmega-python-mcumgr",
    version=VERSION,
    author="Lohmega",
    author_email="info@lohmega.com",
    entry_points={"console_scripts": ["mcumgr=mcumgr.cli:main"]},
    description="Library and command line tool for mcumgr protocol(s)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/lohmega/python-mcumgr",
    packages=setuptools.find_packages(exclude=["test", "test.*"]),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
    ],
    install_requires=[
        "cbor2",
        "crcmod",
        # for serial/nlip transport
        "pyserial",
        # for BLE transport
        "bleak >= 0.20",
    ],
    python_requires=">=3.8",
)
