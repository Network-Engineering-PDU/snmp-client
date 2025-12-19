import setuptools


with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="ttsnmp",
    version="1.0.0",
    author="Tychetools",
    description="TycheTools NetSNMP pass_persist helper",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://bitbucket.org/tychetools/snmp-client/src/master/",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
    ],
    python_requires=">=3.8",
    install_requires=[
        "requests==2.28.1",
    ],
    entry_points={
        "console_scripts": {
            "ttsnmpd_helper = ttsnmp.__init__:snmpd_helper",
            "nesnmpd_helper = ttsnmp.__init__:ne_snmpd_helper",
        }
    },
    scripts=[]
)
