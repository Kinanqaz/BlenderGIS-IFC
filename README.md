BlenderGIS-IFC
==============

BlenderGIS-IFC is a fork of the original [BlenderGIS](https://github.com/domlysz/BlenderGIS) by domlysz, extended with **IFC (Industry Foundation Classes)** integration for streamlined GIS-to-BIM workflows.

- **Blender minimum version required:** v2.83
- **Current version:** 2.2.14

> **Note:** Since 2022, the OpenTopography web service requires an API key. Please register at [opentopography.org](https://opentopography.org) and request a key. This service is still free.

> **Note:** A [MapTiler API key](https://cloud.maptiler.com/) is required for searching and adding Coordinate Reference Systems (CRS) through the EPSG database. Set it in the add-on preferences.

---

## What's New in BlenderGIS-IFC

### IFC Class Assignment for Shapefile Import
The biggest addition is seamless **IFC integration** when importing ESRI Shapefiles. This bridges GIS data directly into BIM workflows using [Bonsai](https://bonsaibim.org/) (formerly BlenderBIM).

- **Assign IFC Class:** When importing a shapefile, enable *"Assign IFC Class"* and specify the IFC class (e.g., `IfcBuilding`, `IfcSite`, `IfcBuildingElementProxy`).
- **Predefined & User-Defined Types:** Optionally set IFC predefined types and user-defined types.
- **Automatic Property Sets (Psets):** When importing with *"Separate Objects"* enabled, all shapefile attribute fields are automatically mapped to an IFC Property Set called `Pset_GIS_Attributes` on each imported object.
- **Bonsai Integration:** Works directly with Bonsai's API to create proper IFC elements, export mesh geometry as IFC tessellation representations, and update the IFC navigation panel.
- **Fallback Mode:** If Bonsai is not installed, IFC class information is stored as custom object properties.

See [`IFC_IMPORT_FEATURE.md`](./IFC_IMPORT_FEATURE.md) for detailed usage instructions and workflow examples.

### Batch Shapefile Import
Import multiple shapefiles in one go via **Shapefile Batch (.shp)** in the GIS menu.

### MapTiler Coordinates API
Migrated CRS search from epsg.io to the [MapTiler Coordinates API](https://cloud.maptiler.com/). Requires a free API key configured in preferences.

### Compatibility & Fixes
- **Blender 4.x & 5.x compatibility** — guards against breaking changes on future major Blender releases.
- **macOS Apple Silicon (ARM)** support fixes for image I/O.
- **Pillow** deprecation fixes for recent Python versions.
- **pyshp** and reprojection engine updates.

---

## Functionalities Overview

### GIS Datafile Import
Import in Blender most common GIS data formats: Shapefile vector, raster image, GeoTIFF DEM, OpenStreetMap XML, and ESRI ASCII Grid.

There are a lot of possibilities to create a 3D terrain from geographic data; check the [Flowchart](https://raw.githubusercontent.com/wiki/domlysz/blenderGIS/flowchart.jpg) for an overview.

Example: import vector contour lines, create faces by triangulation, and put a topographic raster texture.

![](https://raw.githubusercontent.com/wiki/domlysz/blenderGIS/Blender28x/gif/bgis_demo_delaunay.gif)

### Grab Geodata Directly from the Web
Display dynamic web maps inside Blender's 3D view, request OpenStreetMap data (buildings, roads, etc.), and get true elevation data from the NASA SRTM mission.

![](https://raw.githubusercontent.com/wiki/domlysz/blenderGIS/Blender28x/gif/bgis_demo_webdata.gif)

### More Features
- Manage georeferencing information of a scene.
- Compute a terrain mesh by Delaunay triangulation.
- Drop objects on a terrain mesh.
- Make terrain analysis using shader nodes.
- Setup new cameras from geotagged photos.
- Setup a camera to render a new georeferenced raster.
- **IFC class assignment and Property Set generation** for BIM workflows.

---

## Requirements for IFC Features

To use the full IFC functionality, install and enable:

- **[Bonsai (BlenderBIM)](https://bonsaibim.org/)** — the free and open-source BIM authoring tool for Blender.
- **IfcOpenShell** — usually bundled with Bonsai; required for Property Set creation.

Both must be active before importing shapefiles with IFC assignment enabled.

---

## Links

- [Original BlenderGIS Wiki](https://github.com/domlysz/BlenderGIS/wiki/Home)
- [Original BlenderGIS FAQ](https://github.com/domlysz/BlenderGIS/wiki/FAQ)
- [Original Quick Start Guide](https://github.com/domlysz/BlenderGIS/wiki/Quick-start)
- [Flowchart](https://raw.githubusercontent.com/wiki/domlysz/blenderGIS/flowchart.jpg)
- [IFC Import Feature Documentation](./IFC_IMPORT_FEATURE.md)
