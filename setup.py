from setuptools import setup

setup(name='visilens',
    version='0.1',
    description='Module for modeling gravitational lensing systems in which the data are not images but interferometric visibilities.',
    classifiers=[
    'Development Status :: 7 - Inactive',
    'License :: OSI Approved :: MIT License',
    'Programming Language :: Python :: 2.7',
    'Topic :: Scientific/Engineering :: Astronomy',
    ],
    keywords='visilens interferometry alma casa mc mc radioastronomy',
    url='http://github.com/masolimano/visilens',
    author='jspilker',
    license='MIT',
    packages=['visilens'],
    install_requires=[
	'numpy',
	'scipy',
	'matplotlib',
	'astropy',
	'Pillow',
	'emcee',
    ],
    include_package_data=True,
    zip_safe=False)
