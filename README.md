# DRAINMOD_DWR_Assessment
This repository contains python scripts that can be used to assess drainage water recycling implementation per county in Central Illinois corn systems.
The scripts included in this repository should be run in the following order:
1. Weather
2. Soil
3. Automated DM
4. DM Summary
Outputs include .csv files for drainage treatment analysis, water stress, and yields from data ranges between 1996-2026.
The DRAINMOD_GRID folder must be copied into the Drainmod7 application folder after installation.
The user must rewrite certain aspects of scripts via debugging process during each run such as:
--> personalized Google Earth Engine project
--> file path directories
--> desired county
