# -*- coding:utf-8 -*-
import os, sys, time
import random
import bpy
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty
from bpy.types import Operator
import bmesh
import math
from mathutils import Vector

import logging
log = logging.getLogger(__name__)

from ..core.lib.shapefile import Reader as shpReader

from ..geoscene import GeoScene, georefManagerLayout
from ..prefs import PredefCRS
from ..core import BBOX
from ..core.proj import Reproj
from ..core.utils import perf_clock

from .utils import adjust3Dview, getBBOX, DropToGround

# Try to import Bonsai and ifcopenshell for IFC integration
IFCOPENSHELL_AVAILABLE = False
try:
	import ifcopenshell
	IFCOPENSHELL_AVAILABLE = True
	log.info("IfcOpenShell detected - IFC Pset creation available")
except ImportError:
	log.info("IfcOpenShell not detected - IFC Pset creation unavailable")

# Try to import Bonsai for IFC integration
BONSAI_AVAILABLE = False
try:
	import bonsai.tool as tool
	BONSAI_AVAILABLE = True
	log.info("Bonsai detected - IFC integration available")
except ImportError:
	log.info("Bonsai not detected - IFC integration will use custom properties only")


def _prepare_pset_properties(properties):
	"""Convert shapefile field values to IFC-compatible property dict."""
	pset_props = {}
	for key, value in properties.items():
		if isinstance(value, bytes):
			value = value.decode('utf-8', errors='replace').strip()
		elif value is None:
			value = ""
		elif isinstance(value, (int, float)):
			value = value
		else:
			value = str(value)
		pset_props[key] = value
	return pset_props


def _create_pset_for_element(model, element, properties):
	"""Create a Pset_GIS_Attributes on the given IFC element."""
	if not properties or not IFCOPENSHELL_AVAILABLE:
		return
	try:
		pset_props = _prepare_pset_properties(properties)
		if not pset_props:
			return
		pset = ifcopenshell.api.pset.add_pset(model, product=element, name="Pset_GIS_Attributes")
		ifcopenshell.api.pset.edit_pset(model, pset=pset, properties=pset_props)
		log.info(f"Created Pset 'Pset_GIS_Attributes' with {len(pset_props)} properties on element {element.id()}")
	except Exception as e:
		log.error(f"Failed to create IFC Pset for element {element.id()}: {e}", exc_info=True)


def _generate_random_color():
	"""Generate a random color with good visibility."""
	# Use golden ratio to generate visually distinct colors
	hue = random.random()
	saturation = 0.6 + random.random() * 0.4  # 0.6-1.0 for vibrant colors
	value = 0.7 + random.random() * 0.3  # 0.7-1.0 for bright colors

	# Convert HSV to RGB
	import colorsys
	rgb = colorsys.hsv_to_rgb(hue, saturation, value)
	return (rgb[0], rgb[1], rgb[2], 1.0)  # RGBA


def _get_color_for_field_value(value, color_map):
	"""Get or generate a color for a specific field value."""
	if value in color_map:
		return color_map[value]
	else:
		color = _generate_random_color()
		color_map[value] = color
		return color


def _assign_ifc_surface_style(model, element, color):
	"""Assign an IfcSurfaceStyle with the given color to an IFC element."""
	if not IFCOPENSHELL_AVAILABLE:
		return False
	try:
		# Create a material
		material = ifcopenshell.api.material.add_material(model, name=f"Style_{color[0]:.2f}_{color[1]:.2f}_{color[2]:.2f}")

		# Create a surface style
		style = ifcopenshell.api.style.add_style(model, name="SurfaceStyle")

		# Set the surface style rendering properties
		ifcopenshell.api.style.add_surface_style(
			model,
			style=style,
			ifc_class="IfcSurfaceStyleRendering",
			properties={
				"SurfaceColour": {
					"Name": None,
					"Red": color[0],
					"Green": color[1],
					"Blue": color[2]
				},
				"ReflectanceMethod": "FLAT",
				"Transparency": 1.0 - color[3]  # Alpha to transparency
			}
		)

		# Assign the style to the material
		ifcopenshell.api.material.assign_style(model, material=material, style=style)

		# Assign the material to the element
		ifcopenshell.api.material.assign_material(model, product=element, material=material)

		log.info(f"Assigned IfcSurfaceStyle to element {element.id()} with color RGB({color[0]:.2f}, {color[1]:.2f}, {color[2]:.2f})")
		return True
	except Exception as e:
		log.error(f"Failed to assign IfcSurfaceStyle to element {element.id()}: {e}", exc_info=True)
		return False


def assign_blender_material(obj, color):
	"""Assign a Blender material with the given color to an object."""
	try:
		# Create or get material
		mat_name = f"Color_{color[0]:.2f}_{color[1]:.2f}_{color[2]:.2f}"
		if mat_name in bpy.data.materials:
			mat = bpy.data.materials[mat_name]
		else:
			mat = bpy.data.materials.new(name=mat_name)
			mat.use_nodes = True
			bsdf = mat.node_tree.nodes["Principled BSDF"]
			bsdf.inputs["Base Color"].default_value = color
			bsdf.inputs["Alpha"].default_value = color[3]

		# Assign material to object
		if obj.data.materials:
			obj.data.materials[0] = mat
		else:
			obj.data.materials.append(mat)

		log.info(f"Assigned Blender material '{mat_name}' to object '{obj.name}'")
	except Exception as e:
		log.error(f"Failed to assign Blender material to object {obj.name}: {e}", exc_info=True)


def assign_ifc_class_to_object(obj, ifc_class, predefined_type="", user_defined_type="", properties=None):
	"""Assign IFC class to a Blender object using Bonsai API if available, otherwise use custom properties.
	If properties dict is provided and Bonsai is active, creates an IFC Pset with the shapefile field values."""
	properties = properties or {}
	if BONSAI_AVAILABLE:
		# Save object name before any operations (Bonsai may rename the object)
		obj_name = obj.name
		try:
			# Check if there's an active IFC project
			model = tool.Ifc.get()
			if model is None:
				log.warning("No active IFC project in Bonsai, falling back to custom properties")
				obj["IfcClass"] = ifc_class
				if predefined_type:
					obj["IfcPredefinedType"] = predefined_type
				if user_defined_type:
					obj["IfcUserDefinedType"] = user_defined_type
				for key, value in properties.items():
					obj[key] = value
				return

			# Record existing IFC elements before assignment so we can identify
			# the newly created element afterwards
			existing_elements = set(model)

			# Use Bonsai's built-in assign_class operator to properly handle
			# object naming, navigation panel updates, and IFC geometry representation
			bpy.ops.bim.assign_class(
				obj=obj_name,
				ifc_class=ifc_class,
				predefined_type=predefined_type or "",
				userdefined_type=user_defined_type or ""
			)

			# Find the newly created IFC element by looking for elements
			# that were not in the model before assign_class was called
			new_element = None
			for element in model:
				if element not in existing_elements and element.is_a() == ifc_class:
					new_element = element
					break

			if new_element is None:
				# Fallback: search by linked object name if element identity check failed
				for element in model:
					if element.is_a() == ifc_class:
						linked_obj = tool.Ifc.get_object(element)
						if linked_obj and linked_obj.name == obj_name:
							new_element = element
							break

			if new_element is not None:
				_create_pset_for_element(model, new_element, properties)
			else:
				log.warning(f"Could not identify new IFC element for '{obj_name}' to create Pset")

			log.info(f"Assigned IFC class '{ifc_class}' to object '{obj_name}' using Bonsai API")
		except Exception as e:
			log.error(f"Failed to assign IFC class using Bonsai API: {e}", exc_info=True)
			# Try to find the object by original name in case it was renamed
			try:
				if obj_name in bpy.data.objects:
					obj = bpy.data.objects[obj_name]
					obj["IfcClass"] = ifc_class
					if predefined_type:
						obj["IfcPredefinedType"] = predefined_type
					if user_defined_type:
						obj["IfcUserDefinedType"] = user_defined_type
					for key, value in properties.items():
						obj[key] = value
			except Exception as fallback_e:
				log.error(f"Could not set fallback IFC properties: {fallback_e}")
	else:
		# Bonsai not available, use custom properties
		obj["IfcClass"] = ifc_class
		if predefined_type:
			obj["IfcPredefinedType"] = predefined_type
		if user_defined_type:
			obj["IfcUserDefinedType"] = user_defined_type
		for key, value in properties.items():
			obj[key] = value
		log.info(f"Assigned IFC class '{ifc_class}' to object '{obj.name}' using custom properties")

PKG, SUBPKG = __package__.split('.', maxsplit=1)

# Temporary storage for batch import file paths
_batch_files = []

featureType={
0:'Null',
1:'Point',
3:'PolyLine',
5:'Polygon',
8:'MultiPoint',
11:'PointZ',
13:'PolyLineZ',
15:'PolygonZ',
18:'MultiPointZ',
21:'PointM',
23:'PolyLineM',
25:'PolygonM',
28:'MultiPointM',
31:'MultiPatch'
}


"""
dbf fields type:
	C is ASCII characters
	N is a double precision integer limited to around 18 characters in length
	D is for dates in the YYYYMMDD format, with no spaces or hyphens between the sections
	F is for floating point numbers with the same length limits as N
	L is for logical data which is stored in the shapefile's attribute table as a short integer as a 1 (true) or a 0 (false).
	The values it can receive are 1, 0, y, n, Y, N, T, F or the python builtins True and False
"""


class IMPORTGIS_OT_shapefile_file_dialog(Operator):
	"""Select shp file, loads the fields and start importgis.shapefile_props_dialog operator"""

	bl_idname = "importgis.shapefile_file_dialog"
	bl_description = 'Import ESRI shapefile (.shp)'
	bl_label = "Import SHP"
	bl_options = {'INTERNAL'}

	# Import dialog properties
	filepath: StringProperty(
		name="File Path",
		description="Filepath used for importing the file",
		maxlen=1024,
		subtype='FILE_PATH' )

	filename_ext = ".shp"

	filter_glob: StringProperty(
			default = "*.shp",
			options = {'HIDDEN'} )

	def invoke(self, context, event):
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

	def draw(self, context):
		layout = self.layout
		layout.label(text="Options will be available")
		layout.label(text="after selecting a file")

	def execute(self, context):
		if os.path.exists(self.filepath):
			bpy.ops.importgis.shapefile_props_dialog('INVOKE_DEFAULT', filepath=self.filepath)
		else:
			self.report({'ERROR'}, "Invalid filepath")
		return{'FINISHED'}



class IMPORTGIS_OT_shapefile_props_dialog(Operator):
	"""Shapefile importer properties dialog"""

	bl_idname = "importgis.shapefile_props_dialog"
	bl_description = 'Import ESRI shapefile (.shp)'
	bl_label = "Import SHP"
	bl_options = {"INTERNAL"}

	filepath: StringProperty()

	#special function to auto redraw an operator popup called through invoke_props_dialog
	def check(self, context):
		return True

	def listFields(self, context):
		fieldsItems = []
		try:
			shp = shpReader(self.filepath)
		except Exception as e:
			log.warning("Unable to read shapefile fields", exc_info=True)
			return fieldsItems
		fields = [field for field in shp.fields if field[0] != 'DeletionFlag'] #ignore default DeletionFlag field
		for i, field in enumerate(fields):
			#put each item in a tuple (key, label, tooltip)
			fieldsItems.append( (field[0], field[0], '') )
		return fieldsItems

	# Shapefile CRS definition
	def listPredefCRS(self, context):
		return PredefCRS.getEnumItems()

	def listObjects(self, context):
		objs = []
		for index, object in enumerate(bpy.context.scene.objects):
			if object.type == 'MESH':
				#put each object in a tuple (key, label, tooltip) and add this to the objects list
				objs.append((object.name, object.name, "Object named " + object.name))
		return objs

	reprojection: BoolProperty(
			name="Specifiy shapefile CRS",
			description="Specifiy shapefile CRS if it's different from scene CRS",
			default=False )

	shpCRS: EnumProperty(
		name = "Shapefile CRS",
		description = "Choose a Coordinate Reference System",
		items = listPredefCRS)

	# Elevation source
	vertsElevSource: EnumProperty(
			name="Elevation source",
			description="Select the source of vertices z value",
			items=[
			('NONE', 'None', "Flat geometry"),
			('GEOM', 'Geometry', "Use z value from shape geometry if exists"),
			('FIELD', 'Field', "Extract z elevation value from an attribute field"),
			('OBJ', 'Object', "Get z elevation value from an existing ground mesh")
			],
			default='GEOM')

	# Elevation object
	objElevLst: EnumProperty(
		name="Elev. object",
		description="Choose the mesh from which extract z elevation",
		items=listObjects )

	# Elevation field
	'''
	useFieldElev: BoolProperty(
			name="Elevation from field",
			description="Extract z elevation value from an attribute field",
			default=False )
	'''
	fieldElevName: EnumProperty(
		name = "Elev. field",
		description = "Choose field",
		items = listFields )

	#Extrusion field
	useFieldExtrude: BoolProperty(
			name="Extrusion from field",
			description="Extract z extrusion value from an attribute field",
			default=False )

	fieldExtrudeName: EnumProperty(
		name = "Field",
		description = "Choose field",
		items = listFields )

	#Extrusion axis
	extrusionAxis: EnumProperty(
			name="Extrude along",
			description="Select extrusion axis",
			items=[ ('Z', 'z axis', "Extrude along Z axis"),
			('NORMAL', 'Normal', "Extrude along normal")] )

	#Create separate objects
	separateObjects: BoolProperty(
			name="Separate objects",
			description="Warning : can be very slow with lot of features",
			default=False )

	#Name objects from field
	useFieldName: BoolProperty(
			name="Object name from field",
			description="Extract name for created objects from an attribute field",
			default=False )
	fieldObjName: EnumProperty(
		name = "Field",
		description = "Choose field",
		items = listFields )

	# IFC Assignment
	assignIFC: BoolProperty(
			name="Assign IFC Class",
			description="Assign IFC class to imported objects for Bonsai/BlenderBIM",
			default=False )
	
	ifcClass: StringProperty(
			name="IFC Class",
			description="IFC class name (e.g., IfcBuilding, IfcSite, IfcBuildingElementProxy)",
			default="IfcBuildingElementProxy" )
	
	ifcPredefinedType: StringProperty(
			name="IFC Predefined Type",
			description="IFC predefined type (optional)",
			default="" )
	
	ifcUserDefinedType: StringProperty(
			name="IFC User Defined Type",
			description="IFC user defined type (optional)",
			default="" )

	# Random coloring
	useRandomColor: BoolProperty(
			name="Random color by field",
			description="Assign random colors (IfcSurfaceStyles) to objects based on unique field values",
			default=False )

	fieldColorName: EnumProperty(
		name = "Color field",
		description = "Choose field to base random coloring on",
		items = listFields )


	def draw(self, context):
		#Function used by blender to draw the panel.
		scn = context.scene
		layout = self.layout

		#
		layout.prop(self, 'vertsElevSource')
		#
		#layout.prop(self, 'useFieldElev')
		if self.vertsElevSource == 'FIELD':
			layout.prop(self, 'fieldElevName')
		elif self.vertsElevSource == 'OBJ':
			layout.prop(self, 'objElevLst')
		#
		layout.prop(self, 'useFieldExtrude')
		if self.useFieldExtrude:
			layout.prop(self, 'fieldExtrudeName')
			layout.prop(self, 'extrusionAxis')
		#
		layout.prop(self, 'separateObjects')
		if self.separateObjects:
			layout.prop(self, 'useFieldName')
		else:
			self.useFieldName = False
		if self.separateObjects and self.useFieldName:
			layout.prop(self, 'fieldObjName')
		#
		layout.separator()
		layout.prop(self, 'assignIFC')
		if self.assignIFC:
			layout.prop(self, 'ifcClass')
			layout.prop(self, 'ifcPredefinedType')
			layout.prop(self, 'ifcUserDefinedType')
		#
		layout.prop(self, 'useRandomColor')
		if self.useRandomColor:
			layout.prop(self, 'fieldColorName')
		#
		geoscn = GeoScene()
		#geoscnPrefs = context.preferences.addons['geoscene'].preferences
		if geoscn.isPartiallyGeoref:
			layout.prop(self, 'reprojection')
			if self.reprojection:
				self.shpCRSInputLayout(context)
			#
			georefManagerLayout(self, context)
		else:
			self.shpCRSInputLayout(context)


	def shpCRSInputLayout(self, context):
		layout = self.layout
		row = layout.row(align=True)
		#row.prop(self, "shpCRS", text='CRS')
		split = row.split(factor=0.35, align=True)
		split.label(text='CRS:')
		split.prop(self, "shpCRS", text='')
		row.operator("bgis.add_predef_crs", text='', icon='ADD')


	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self)

	def execute(self, context):

		#elevField = self.fieldElevName if self.useFieldElev else ""
		elevField = self.fieldElevName if self.vertsElevSource == 'FIELD' else ""
		extrudField = self.fieldExtrudeName if self.useFieldExtrude else ""
		nameField = self.fieldObjName if self.useFieldName else ""
		if self.vertsElevSource == 'OBJ':
			if not self.objElevLst:
				self.report({'ERROR'}, "No elevation object")
				return {'CANCELLED'}
			else:
				objElevName = self.objElevLst
		else:
			objElevName = '' #will not be used

		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		if geoscn.isGeoref:
			if self.reprojection:
				shpCRS = self.shpCRS
			else:
				shpCRS = geoscn.crs
		else:
			shpCRS = self.shpCRS

		# Prepare IFC parameters
		ifcClass = self.ifcClass if self.assignIFC else ""
		ifcPredefinedType = self.ifcPredefinedType if self.assignIFC else ""
		ifcUserDefinedType = self.ifcUserDefinedType if self.assignIFC else ""

		# Prepare coloring parameters
		colorField = self.fieldColorName if self.useRandomColor else ""

		log.info(f"IFC Assignment: assignIFC={self.assignIFC}, ifcClass={ifcClass}")
		log.info(f"Random coloring: useRandomColor={self.useRandomColor}, colorField={colorField}")

		try:
			bpy.ops.importgis.shapefile('INVOKE_DEFAULT', filepath=self.filepath, shpCRS=shpCRS, elevSource=self.vertsElevSource,
				fieldElevName=elevField, objElevName=objElevName, fieldExtrudeName=extrudField, fieldObjName=nameField,
				extrusionAxis=self.extrusionAxis, separateObjects=self.separateObjects,
				ifcClass=ifcClass, ifcPredefinedType=ifcPredefinedType, ifcUserDefinedType=ifcUserDefinedType,
				useRandomColor=self.useRandomColor, fieldColorName=colorField)
		except Exception as e:
			log.error('Shapefile import fails', exc_info=True)
			self.report({'ERROR'}, 'Shapefile import fails, check logs.')
			return {'CANCELLED'}

		return{'FINISHED'}


class IMPORTGIS_OT_shapefile(Operator):
	"""Import from ESRI shapefile file format (.shp)"""

	bl_idname = "importgis.shapefile" # important since its how bpy.ops.import.shapefile is constructed (allows calling operator from python console or another script)
	#bl_idname rules: must contain one '.' (dot) charactere, no capital letters, no reserved words (like 'import')
	bl_description = 'Import ESRI shapefile (.shp)'
	bl_label = "Import SHP"
	bl_options = {"UNDO"}

	filepath: StringProperty()

	shpCRS: StringProperty(name = "Shapefile CRS", description = "Coordinate Reference System")

	elevSource: StringProperty(name = "Elevation source", description = "Elevation source", default='GEOM') # [NONE, GEOM, OBJ, FIELD]
	objElevName: StringProperty(name = "Elevation object name", description = "")

	fieldElevName: StringProperty(name = "Elevation field", description = "Field name")
	fieldExtrudeName: StringProperty(name = "Extrusion field", description = "Field name")
	fieldObjName: StringProperty(name = "Objects names field", description = "Field name")

	# IFC Assignment
	ifcClass: StringProperty(name = "IFC Class", description = "IFC class name for Bonsai/BlenderBIM", default="")
	ifcPredefinedType: StringProperty(name = "IFC Predefined Type", description = "IFC predefined type", default="")
	ifcUserDefinedType: StringProperty(name = "IFC User Defined Type", description = "IFC user defined type", default="")

	# Random coloring
	useRandomColor: BoolProperty(name = "Random color by field", description = "Assign random colors based on field values", default=False)
	fieldColorName: StringProperty(name = "Color field", description = "Field name for random coloring", default="")

	#Extrusion axis
	extrusionAxis: EnumProperty(
			name="Extrude along",
			description="Select extrusion axis",
			items=[ ('Z', 'z axis', "Extrude along Z axis"),
			('NORMAL', 'Normal', "Extrude along normal")]
			)
	#Create separate objects
	separateObjects: BoolProperty(
			name="Separate objects",
			description="Import to separate objects instead one large object",
			default=False
			)

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def __del__(self):
		bpy.context.window.cursor_set('DEFAULT')

	def execute(self, context):

		prefs = bpy.context.preferences.addons[PKG].preferences

		log.info(f"IFC parameters received: ifcClass={self.ifcClass}, ifcPredefinedType={self.ifcPredefinedType}, ifcUserDefinedType={self.ifcUserDefinedType}")

		#Set cursor representation to 'loading' icon
		w = context.window
		w.cursor_set('WAIT')
		t0 = perf_clock()

		bpy.ops.object.select_all(action='DESELECT')

		#Path
		shpName = os.path.basename(self.filepath)[:-4]

		#Get shp reader
		log.info("Read shapefile...")
		try:
			shp = shpReader(self.filepath)
		except Exception as e:
			log.error("Unable to read shapefile", exc_info=True)
			self.report({'ERROR'}, "Unable to read shapefile, check logs")
			return {'CANCELLED'}

		#Check shape type
		shpType = featureType[shp.shapeType]
		log.info('Feature type : ' + shpType)
		if shpType not in ['Point','PolyLine','Polygon','PointZ','PolyLineZ','PolygonZ']:
			self.report({'ERROR'}, "Cannot process multipoint, multipointZ, pointM, polylineM, polygonM and multipatch feature type")
			return {'CANCELLED'}

		if self.elevSource != 'FIELD':
			self.fieldElevName = ''

		if self.elevSource == 'OBJ':
			scn = bpy.context.scene
			elevObj = scn.objects[self.objElevName]
			rayCaster = DropToGround(scn, elevObj)

		#Get fields
		fields = [field for field in shp.fields if field[0] != 'DeletionFlag'] #ignore default DeletionFlag field
		fieldsNames = [field[0] for field in fields]
		log.debug("DBF fields : "+str(fieldsNames))

		if self.separateObjects or self.fieldElevName or self.fieldObjName or self.fieldExtrudeName:
			self.useDbf = True
		else:
			self.useDbf = False

		if self.fieldObjName and self.separateObjects:
			try:
				nameFieldIdx = fieldsNames.index(self.fieldObjName)
			except Exception as e:
				log.error('Unable to find name field', exc_info=True)
				self.report({'ERROR'}, "Unable to find name field")
				return {'CANCELLED'}

		if self.fieldElevName:
			try:
				zFieldIdx = fieldsNames.index(self.fieldElevName)
			except Exception as e:
				log.error('Unable to find elevation field', exc_info=True)
				self.report({'ERROR'}, "Unable to find elevation field")
				return {'CANCELLED'}

			if fields[zFieldIdx][1] not in ['N', 'F', 'L'] :
				self.report({'ERROR'}, "Elevation field do not contains numeric values")
				return {'CANCELLED'}

		if self.fieldExtrudeName:
			try:
				extrudeFieldIdx = fieldsNames.index(self.fieldExtrudeName)
			except ValueError:
				log.error('Unable to find extrusion field', exc_info=True)
				self.report({'ERROR'}, "Unable to find extrusion field")
				return {'CANCELLED'}

			if fields[extrudeFieldIdx][1] not in ['N', 'F', 'L'] :
				self.report({'ERROR'}, "Extrusion field do not contains numeric values")
				return {'CANCELLED'}

		# Color field lookup
		colorFieldIdx = None
		colorMap = {}
		if self.useRandomColor and self.fieldColorName:
			try:
				colorFieldIdx = fieldsNames.index(self.fieldColorName)
				log.info(f"Color field '{self.fieldColorName}' found at index {colorFieldIdx}")
			except ValueError:
				log.error('Unable to find color field', exc_info=True)
				self.report({'ERROR'}, "Unable to find color field")
				return {'CANCELLED'}

		#Get shp and scene georef infos
		shpCRS = self.shpCRS
		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		scale = geoscn.scale #TODO

		if not geoscn.hasCRS: #if not geoscn.isGeoref:
			try:
				geoscn.crs = shpCRS
			except Exception as e:
				log.error("Cannot set scene crs", exc_info=True)
				self.report({'ERROR'}, "Cannot set scene crs, check logs for more infos")
				return {'CANCELLED'}

		#Init reprojector class
		if geoscn.crs != shpCRS:
			log.info("Data will be reprojected from {} to {}".format(shpCRS, geoscn.crs))
			try:
				rprj = Reproj(shpCRS, geoscn.crs)
			except Exception as e:
				log.error('Reprojection fails', exc_info=True)
				self.report({'ERROR'}, "Unable to reproject data, check logs for more infos.")
				return {'CANCELLED'}
			if rprj.iproj == 'EPSGIO':
				if shp.numRecords > 100:
					self.report({'ERROR'}, "Reprojection through online epsg.io engine is limited to 100 features. \nPlease install GDAL or pyproj module.")
					return {'CANCELLED'}

		#Get bbox
		bbox = BBOX(shp.bbox)
		if geoscn.crs != shpCRS:
			bbox = rprj.bbox(bbox)

		#Get or set georef dx, dy
		if not geoscn.isGeoref:
			dx, dy = bbox.center
			geoscn.setOriginPrj(dx, dy)
		else:
			dx, dy = geoscn.getOriginPrj()

		#Get reader iterator (using iterator avoids loading all data in memory)
		#warn, shp with zero field will return an empty shapeRecords() iterator
		#to prevent this issue, iter only on shapes if there is no field required
		if self.useDbf:
			#Note: using shapeRecord solve the issue where number of shapes does not match number of table records
			#because it iter only on features with geom and record
			shpIter = shp.iterShapeRecords()
		else:
			shpIter = shp.iterShapes()
		nbFeats = shp.numRecords

		#Create an empty BMesh
		bm = bmesh.new()
		#Extrusion is exponentially slow with large bmesh
		#it's fastest to extrude a small bmesh and then join it to a final large bmesh
		if not self.separateObjects and self.fieldExtrudeName:
			finalBm = bmesh.new()

		progress = -1

		if self.separateObjects:
			layer = bpy.data.collections.new(shpName)
			context.scene.collection.children.link(layer)

		#Main iteration over features
		for i, feat in enumerate(shpIter):

			if self.useDbf:
				shape = feat.shape
				record = feat.record
			else:
				shape = feat

			#Progress infos
			pourcent = round(((i+1)*100)/nbFeats)
			if pourcent in list(range(0, 110, 10)) and pourcent != progress:
				progress = pourcent
				if pourcent == 100:
					print(str(pourcent)+'%')
				else:
					print(str(pourcent), end="%, ")
				sys.stdout.flush() #we need to flush or it won't print anything until after the loop has finished

			#Deal with multipart features
			#If the shape record has multiple parts, the 'parts' attribute will contains the index of
			#the first point of each part. If there is only one part then a list containing 0 is returned
			if (shpType == 'PointZ' or shpType == 'Point'): #point layer has no attribute 'parts'
				partsIdx = [0]
			else:
				try: #prevent "_shape object has no attribute parts" error
					partsIdx = shape.parts
				except Exception as e:
					log.warning('Cannot access "parts" attribute for feature {} : {}'.format(i, e))
					partsIdx = [0]
			nbParts = len(partsIdx)

			#Get list of shape's points
			pts = shape.points
			nbPts = len(pts)

			#Skip null geom
			if nbPts == 0:
				continue #go to next iteration of the loop

			#Reproj geom
			if geoscn.crs != shpCRS:
				pts = rprj.pts(pts)

			#Get extrusion offset
			if self.fieldExtrudeName:
				try:
					offset = float(record[extrudeFieldIdx])
				except Exception as e:
					log.warning('Cannot extract extrusion value for feature {} : {}'.format(i, e))
					offset = 0 #null values will be set to zero

			#Iter over parts
			for j in range(nbParts):

				# EXTRACT 3D GEOM

				geom = [] #will contains a list of 3d points

				#Find first and last part index
				idx1 = partsIdx[j]
				if j+1 == nbParts:
					idx2 = nbPts
				else:
					idx2 = partsIdx[j+1]

				#Build 3d geom
				for k, pt in enumerate(pts[idx1:idx2]):

					if self.elevSource == 'OBJ':
						rcHit = rayCaster.rayCast(x=pt[0]-dx, y=pt[1]-dy)
						z = rcHit.loc.z #will be automatically set to zero if not rcHit.hit

					elif self.elevSource == 'FIELD':
						try:
							z = float(record[zFieldIdx])
						except Exception as e:
							log.warning('Cannot extract elevation value for feature {} : {}'.format(i, e))
							z = 0 #null values will be set to zero

					elif shpType[-1] == 'Z' and self.elevSource == 'GEOM':
						z = shape.z[idx1:idx2][k]

					else:
						z = 0

					geom.append((pt[0], pt[1], z))

				#Shift coords
				geom = [(pt[0]-dx, pt[1]-dy, pt[2]) for pt in geom]


				# BUILD BMESH

				# POINTS
				if (shpType == 'PointZ' or shpType == 'Point'):
					vert = [bm.verts.new(pt) for pt in geom]
					#Extrusion
					if self.fieldExtrudeName and offset > 0:
						vect = (0, 0, offset) #along Z
						result = bmesh.ops.extrude_vert_indiv(bm, verts=vert)
						verts = result['verts']
						bmesh.ops.translate(bm, verts=verts, vec=vect)

				# LINES
				if (shpType == 'PolyLine' or shpType == 'PolyLineZ'):
					verts = [bm.verts.new(pt) for pt in geom]
					edges = []
					for i in range(len(geom)-1):
						edge = bm.edges.new( [verts[i], verts[i+1] ])
						edges.append(edge)
					#Extrusion
					if self.fieldExtrudeName and offset > 0:
						vect = (0, 0, offset) # along Z
						result = bmesh.ops.extrude_edge_only(bm, edges=edges)
						verts = [elem for elem in result['geom'] if isinstance(elem, bmesh.types.BMVert)]
						bmesh.ops.translate(bm, verts=verts, vec=vect)

				# NGONS
				if (shpType == 'Polygon' or shpType == 'PolygonZ'):
					#According to the shapefile spec, polygons points are clockwise and polygon holes are counterclockwise
					#in Blender face is up if points are in anticlockwise order
					geom.reverse() #face up
					geom.pop() #exlude last point because it's the same as first pt
					if len(geom) >= 3: #needs 3 points to get a valid face
						verts = [bm.verts.new(pt) for pt in geom]
						face = bm.faces.new(verts)
						#update normal to avoid null vector
						face.normal_update()
						if face.normal.z < 0: #this is a polygon hole, bmesh cannot handle polygon hole
							pass #TODO
						#Extrusion
						if self.fieldExtrudeName and offset > 0:
							#build translate vector
							if self.extrusionAxis == 'NORMAL':
								normal = face.normal
								vect = normal * offset
							elif self.extrusionAxis == 'Z':
								vect = (0, 0, offset)
							faces = bmesh.ops.extrude_discrete_faces(bm, faces=[face]) #return {'faces': [BMFace]}
							verts = faces['faces'][0].verts
							if self.elevSource == 'OBJ':
								# Making flat roof (TODO add an user input parameter to setup this behaviour)
								z = max([v.co.z for v in verts]) + offset #get max z coord
								for v in verts:
									v.co.z = z
							else:
								##result = bmesh.ops.extrude_face_region(bm, geom=[face]) #return dict {"geom":[BMVert, BMEdge, BMFace]}
								##verts = [elem for elem in result['geom'] if isinstance(elem, bmesh.types.BMVert)] #geom type filter
								bmesh.ops.translate(bm, verts=verts, vec=vect)


			if self.separateObjects:

				if self.fieldObjName:
					try:
						name = record[nameFieldIdx]
					except Exception as e:
						log.warning('Cannot extract name value for feature {} : {}'.format(i, e))
						name = ''
					# null values will return a bytes object containing a blank string of length equal to fields length definition
					if isinstance(name, bytes):
						name = ''
					else:
						name = str(name)
				else:
					name = shpName

				#Calc bmesh bbox
				_bbox = getBBOX.fromBmesh(bm)

				#Calc bmesh geometry origin and translate coords according to it
				#then object location will be set to initial bmesh origin
				#its a work around to bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')
				ox, oy, oz = _bbox.center
				oz = _bbox.zmin
				bmesh.ops.translate(bm, verts=bm.verts, vec=(-ox, -oy, -oz))

				#Create new mesh from bmesh
				mesh = bpy.data.meshes.new(name)
				bm.to_mesh(mesh)
				bm.clear()

				#Validate new mesh
				mesh.validate(verbose=False)

				#Place obj
				obj = bpy.data.objects.new(name, mesh)
				layer.objects.link(obj)
				context.view_layer.objects.active = obj
				obj.select_set(True)
				obj.location = (ox, oy, oz)

				# bpy operators can be very cumbersome when scene contains lot of objects
				# because it cause implicit scene updates calls
				# so we must avoid using operators when created many objects with the 'separate objects' option)
				##bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')

				#write attributes data and build properties dict for IFC Psets
				shapefile_props = {}
				for i, field in enumerate(shp.fields):
					fieldName, fieldType, fieldLength, fieldDecLength = field
					if fieldName != 'DeletionFlag':
						if fieldType in ('N', 'F'):
							v = record[i-1]
							if v is not None:
								#cast to float to avoid overflow error when affecting custom property
								obj[fieldName] = float(record[i-1])
								shapefile_props[fieldName] = float(record[i-1])
						else:
							obj[fieldName] = record[i-1]
							shapefile_props[fieldName] = record[i-1]

				# Assign IFC properties if enabled
				if self.ifcClass:
					assign_ifc_class_to_object(obj, self.ifcClass, self.ifcPredefinedType, self.ifcUserDefinedType, properties=shapefile_props)

				# Apply random coloring if enabled
				if self.useRandomColor and colorFieldIdx is not None:
					try:
						# Get the field value for this feature
						field_value = record[colorFieldIdx]
						if isinstance(field_value, bytes):
							field_value = field_value.decode('utf-8', errors='replace').strip()
						elif field_value is None:
							field_value = ""
						else:
							field_value = str(field_value)

						# Get or generate color for this field value
						color = _get_color_for_field_value(field_value, colorMap)

						# Apply Blender material (always, for visual feedback)
						assign_blender_material(obj, color)

						# If Bonsai is available and IFC class was assigned, also assign IfcSurfaceStyle
						if BONSAI_AVAILABLE and self.ifcClass:
							try:
								model = tool.Ifc.get()
								if model is not None:
									# Find the IFC element linked to this object
									for element in model:
										if element.is_a() == self.ifcClass:
											linked_obj = tool.Ifc.get_object(element)
											if linked_obj and linked_obj.name == obj.name:
												_assign_ifc_surface_style(model, element, color)
												break
							except Exception as e:
								log.warning(f"Could not assign IfcSurfaceStyle for object '{obj.name}': {e}")

					except Exception as e:
						log.warning(f"Failed to apply coloring for feature {i}: {e}")

			elif self.fieldExtrudeName:
				#Join to final bmesh (use from_mesh method hack)
				buff = bpy.data.meshes.new(".temp")
				bm.to_mesh(buff)
				finalBm.from_mesh(buff)
				bpy.data.meshes.remove(buff)
				bm.clear()

		#Write back the whole mesh
		if not self.separateObjects:

			mesh = bpy.data.meshes.new(shpName)

			if self.fieldExtrudeName:
				bm.free()
				bm = finalBm

			if prefs.mergeDoubles:
				bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
			bm.to_mesh(mesh)

			#Finish
			#mesh.update(calc_edges=True)
			mesh.validate(verbose=False) #return true if the mesh has been corrected
			obj = bpy.data.objects.new(shpName, mesh)
			context.scene.collection.objects.link(obj)
			context.view_layer.objects.active = obj
			obj.select_set(True)
			bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')

			# Assign IFC properties if enabled (no individual shapefile record for merged object)
			if self.ifcClass:
				assign_ifc_class_to_object(obj, self.ifcClass, self.ifcPredefinedType, self.ifcUserDefinedType, properties={})

		#free the bmesh
		bm.free()

		t = perf_clock() - t0
		log.info('Build in %f seconds' % t)

		#Adjust grid size
		if prefs.adjust3Dview:
			bbox.shift(-dx, -dy) #convert shapefile bbox in 3d view space
			adjust3Dview(context, bbox)


		return {'FINISHED'}

class IMPORTGIS_OT_shapefile_batch_file_dialog(Operator):
	"""Select multiple shp files and start batch import"""

	bl_idname = "importgis.shapefile_batch_file_dialog"
	bl_description = 'Batch import ESRI shapefiles (.shp)'
	bl_label = "Batch Import SHP"
	bl_options = {'INTERNAL'}

	directory: StringProperty(
		name="Directory",
		description="Directory of selected files",
		subtype='DIR_PATH')

	files: bpy.props.CollectionProperty(
		type=bpy.types.OperatorFileListElement,
		options={'HIDDEN'})

	filter_glob: StringProperty(
			default = "*.shp",
			options = {'HIDDEN'} )

	def invoke(self, context, event):
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

	def draw(self, context):
		layout = self.layout
		layout.label(text="Select multiple .shp files")
		layout.label(text="Options will be available after selection")

	def execute(self, context):
		global _batch_files
		_batch_files = []
		for file_entry in self.files:
			if file_entry.name.lower().endswith('.shp'):
				_batch_files.append(os.path.join(self.directory, file_entry.name))
		if not _batch_files:
			self.report({'ERROR'}, "No .shp files selected")
			return {'CANCELLED'}
		bpy.ops.importgis.shapefile_batch_props_dialog('INVOKE_DEFAULT')
		return{'FINISHED'}


class IMPORTGIS_OT_shapefile_batch_props_dialog(Operator):
	"""Batch shapefile importer properties dialog"""

	bl_idname = "importgis.shapefile_batch_props_dialog"
	bl_description = 'Batch import ESRI shapefiles (.shp)'
	bl_label = "Batch Import SHP"
	bl_options = {"INTERNAL"}

	#special function to auto redraw an operator popup called through invoke_props_dialog
	def check(self, context):
		return True

	def listPredefCRS(self, context):
		return PredefCRS.getEnumItems()

	def listObjects(self, context):
		objs = []
		for index, object in enumerate(bpy.context.scene.objects):
			if object.type == 'MESH':
				objs.append((object.name, object.name, "Object named " + object.name))
		return objs

	reprojection: BoolProperty(
			name="Specifiy shapefile CRS",
			description="Specifiy shapefile CRS if it's different from scene CRS",
			default=False )

	shpCRS: EnumProperty(
		name = "Shapefile CRS",
		description = "Choose a Coordinate Reference System",
		items = listPredefCRS)

	vertsElevSource: EnumProperty(
			name="Elevation source",
			description="Select the source of vertices z value",
			items=[
			('NONE', 'None', "Flat geometry"),
			('GEOM', 'Geometry', "Use z value from shape geometry if exists"),
			('OBJ', 'Object', "Get z elevation value from an existing ground mesh")
			],
			default='GEOM')

	objElevLst: EnumProperty(
		name="Elev. object",
		description="Choose the mesh from which extract z elevation",
		items=listObjects )

	separateObjects: BoolProperty(
			name="Separate objects",
			description="Warning : can be very slow with lot of features",
			default=False )

	assignIFC: BoolProperty(
			name="Assign IFC Class",
			description="Assign IFC class to imported objects for Bonsai/BlenderBIM",
			default=False )

	ifcClass: StringProperty(
			name="IFC Class",
			description="IFC class name (e.g., IfcBuilding, IfcSite, IfcBuildingElementProxy)",
			default="IfcBuildingElementProxy" )

	ifcPredefinedType: StringProperty(
			name="IFC Predefined Type",
			description="IFC predefined type (optional)",
			default="" )

	ifcUserDefinedType: StringProperty(
			name="IFC User Defined Type",
			description="IFC user defined type (optional)",
			default="" )

	def draw(self, context):
		scn = context.scene
		layout = self.layout

		layout.prop(self, 'vertsElevSource')
		if self.vertsElevSource == 'OBJ':
			layout.prop(self, 'objElevLst')

		layout.prop(self, 'separateObjects')

		layout.separator()
		layout.prop(self, 'assignIFC')
		if self.assignIFC:
			layout.prop(self, 'ifcClass')
			layout.prop(self, 'ifcPredefinedType')
			layout.prop(self, 'ifcUserDefinedType')

		geoscn = GeoScene()
		if geoscn.isPartiallyGeoref:
			layout.prop(self, 'reprojection')
			if self.reprojection:
				self.shpCRSInputLayout(context)
			georefManagerLayout(self, context)
		else:
			self.shpCRSInputLayout(context)

	def shpCRSInputLayout(self, context):
		layout = self.layout
		row = layout.row(align=True)
		split = row.split(factor=0.35, align=True)
		split.label(text='CRS:')
		split.prop(self, "shpCRS", text='')
		row.operator("bgis.add_predef_crs", text='', icon='ADD')

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self)

	def execute(self, context):
		global _batch_files

		if self.vertsElevSource == 'OBJ':
			if not self.objElevLst:
				self.report({'ERROR'}, "No elevation object")
				return {'CANCELLED'}
			else:
				objElevName = self.objElevLst
		else:
			objElevName = ''

		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		if geoscn.isGeoref:
			if self.reprojection:
				shpCRS = self.shpCRS
			else:
				shpCRS = geoscn.crs
		else:
			shpCRS = self.shpCRS

		ifcClass = self.ifcClass if self.assignIFC else ""
		ifcPredefinedType = self.ifcPredefinedType if self.assignIFC else ""
		ifcUserDefinedType = self.ifcUserDefinedType if self.assignIFC else ""

		errors = []
		imported = 0
		for filepath in _batch_files:
			try:
				bpy.ops.importgis.shapefile(
					filepath=filepath,
					shpCRS=shpCRS,
					elevSource=self.vertsElevSource,
					fieldElevName='',
					objElevName=objElevName,
					fieldExtrudeName='',
					fieldObjName='',
					extrusionAxis='Z',
					separateObjects=self.separateObjects,
					ifcClass=ifcClass,
					ifcPredefinedType=ifcPredefinedType,
					ifcUserDefinedType=ifcUserDefinedType)
				imported += 1
			except Exception as e:
				log.error('Batch shapefile import fails for %s', filepath, exc_info=True)
				errors.append(os.path.basename(filepath))

		_batch_files = []

		if errors:
			self.report({'WARNING'}, "Imported %i file(s), %i failed: %s" % (imported, len(errors), ', '.join(errors)))
		else:
			self.report({'INFO'}, "Successfully imported %i shapefile(s)" % imported)

		return{'FINISHED'}


classes = [
	IMPORTGIS_OT_shapefile_file_dialog,
	IMPORTGIS_OT_shapefile_props_dialog,
	IMPORTGIS_OT_shapefile,
	IMPORTGIS_OT_shapefile_batch_file_dialog,
	IMPORTGIS_OT_shapefile_batch_props_dialog
]

def register():
	for cls in classes:
		try:
			bpy.utils.register_class(cls)
		except ValueError as e:
			log.warning('{} is already registered, now unregister and retry... '.format(cls))
			bpy.utils.unregister_class(cls)
			bpy.utils.register_class(cls)

def unregister():
	for cls in classes:
		bpy.utils.unregister_class(cls)
