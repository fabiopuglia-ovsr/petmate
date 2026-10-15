from setuptools import setup

package_name = 'petmate_behavior'

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
    description='Behavior engine for Petmate: modes, scheduler, games, docking, POIs.',
    license='Proprietary',
    tests_require=['pytest'],
    entry_points={'console_scripts': ['behavior_node = petmate_behavior.behavior_node:main']},
)
