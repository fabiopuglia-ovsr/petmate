from setuptools import setup

package_name = 'petmate_bridge'

setup(
    name=package_name,
    version='0.1.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Petmate team',
    maintainer_email='dev@petmate.example',
    description='ZeroMQ bridge between pm-core and ROS 2 (INTERFACES.md 3-4).',
    license='Proprietary',
    tests_require=['pytest'],
    entry_points={'console_scripts': ['bridge_node = petmate_bridge.bridge_node:main']},
)
