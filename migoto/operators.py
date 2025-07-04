import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import Operator, AddonPreferences
from bpy_extras.io_utils import ImportHelper, orientation_helper

from .. import __name__ as package_name
from .. import addon_updater_ops
from .datahandling import (
    Fatal,
    apply_vgmap,
    import_pose,
    merge_armatures,
    update_vgmap,
)

from .datastructures import IOOBJOrientationHelper


class ApplyVGMap(Operator, ImportHelper):
    """Apply vertex group map to the selected object"""

    bl_idname = "mesh.migoto_vertex_group_map"
    bl_label = "Apply 3DMigoto vgmap"
    bl_options = {"UNDO"}

    filename_ext = ".vgmap"
    filter_glob: StringProperty(
        default="*.vgmap",
        options={"HIDDEN"},
    ) # type: ignore

    # commit: BoolProperty(
    #        name="Commit to current mesh",
    #        description="Directly alters the vertex groups of the current mesh, rather than performing the mapping at export time",
    #        default=False,
    #        )

    rename: BoolProperty(
        name="Rename existing vertex groups",
        description="Rename existing vertex groups to match the vgmap file",
        default=True,
    ) # type: ignore

    cleanup: BoolProperty(
        name="Remove non-listed vertex groups",
        description="Remove any existing vertex groups that are not listed in the vgmap file",
        default=False,
    ) # type: ignore

    reverse: BoolProperty(
        name="Swap from & to",
        description="Switch the order of the vertex group map - if this mesh is the 'to' and you want to use the bones in the 'from'",
        default=False,
    ) # type: ignore

    suffix: StringProperty(
        name="Suffix",
        description="Suffix to add to the vertex buffer filename when exporting, for bulk exports of a single mesh with multiple distinct vertex group maps",
        default="",
    ) # type: ignore

    def invoke(self, context, event):
        self.suffix = ""
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        try:
            keywords = self.as_keywords(ignore=("filter_glob",))
            apply_vgmap(self, context, **keywords)
        except Fatal as e:
            self.report({"ERROR"}, str(e))
        return {"FINISHED"}


class UpdateVGMap(Operator):
    """Assign new 3DMigoto vertex groups"""

    bl_idname = "mesh.update_migoto_vertex_group_map"
    bl_label = "Assign new 3DMigoto vertex groups"
    bl_options = {"UNDO"}

    vg_step: bpy.props.IntProperty(
        name="Vertex group step",
        description="If used vertex groups are 0,1,2,3,etc specify 1. If they are 0,3,6,9,12,etc specify 3",
        default=1,
        min=1,
    ) # type: ignore

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self)

    def execute(self, context):
        try:
            keywords = self.as_keywords()
            update_vgmap(self, context, **keywords)
        except Fatal as e:
            self.report({"ERROR"}, str(e))
        return {"FINISHED"}


@orientation_helper(axis_forward="-Z", axis_up="Y")
class Import3DMigotoPose(Operator, ImportHelper, IOOBJOrientationHelper):
    """Import a pose from a 3DMigoto constant buffer dump"""

    bl_idname = "armature.migoto_pose"
    bl_label = "Import 3DMigoto Pose"
    bl_options = {"UNDO"}

    filename_ext = ".txt"
    filter_glob: StringProperty(
        default="*.txt",
        options={"HIDDEN"},
    ) # type: ignore

    limit_bones_to_vertex_groups: BoolProperty(
        name="Limit Bones to Vertex Groups",
        description="Limits the maximum number of bones imported to the number of vertex groups of the active object",
        default=True,
    ) # type: ignore

    pose_cb_off: bpy.props.IntVectorProperty(
        name="Bone CB range",
        description="Indicate start and end offsets (in multiples of 4 component values) to find the matrices in the Bone CB",
        default=[0, 0],
        size=2,
        min=0,
    ) # type: ignore

    pose_cb_step: bpy.props.IntProperty(
        name="Vertex group step",
        description="If used vertex groups are 0,1,2,3,etc specify 1. If they are 0,3,6,9,12,etc specify 3",
        default=1,
        min=1,
    ) # type: ignore

    def execute(self, context):
        try:
            keywords = self.as_keywords(ignore=("filter_glob",))
            import_pose(self, context, **keywords)
        except Fatal as e:
            self.report({"ERROR"}, str(e))
        return {"FINISHED"}


class Merge3DMigotoPose(Operator):
    """Merge identically posed bones of related armatures into one"""

    bl_idname = "armature.merge_pose"
    bl_label = "Merge 3DMigoto Poses"
    bl_options = {"UNDO"}

    def execute(self, context):
        try:
            merge_armatures(self, context)
        except Fatal as e:
            self.report({"ERROR"}, str(e))
        return {"FINISHED"}


class DeleteNonNumericVertexGroups(Operator):
    """Remove vertex groups with non-numeric names"""

    bl_idname = "vertex_groups.delete_non_numeric"
    bl_label = "Remove non-numeric vertex groups"
    bl_options = {"UNDO"}

    def execute(self, context):
        try:
            for obj in context.selected_objects:
                for vg in reversed(obj.vertex_groups):
                    if vg.name.isdecimal():
                        continue
                    print("Removing vertex group", vg.name)
                    obj.vertex_groups.remove(vg)
        except Fatal as e:
            self.report({"ERROR"}, str(e))
        return {"FINISHED"}


class Preferences(AddonPreferences):
    """Preferences updater"""

    bl_idname = package_name
    # Addon updater preferences.

    auto_check_update: BoolProperty(
        name="自动检查更新",
        description="如果启用，则使用间隔自动检查更新",
        default=False,
    ) # type: ignore

    updater_interval_months: IntProperty(
        name="月数",
        description="检查更新的间隔月数",
        default=0,
        min=0,
    ) # type: ignore

    updater_interval_days: IntProperty(
        name="天数",
        description="检查更新的间隔天数",
        default=7,
        min=0,
        max=31,
    ) # type: ignore

    updater_interval_hours: IntProperty(
        name="小时",
        description="检查更新的间隔小时",
        default=0,
        min=0,
        max=23,
    ) # type: ignore

    updater_interval_minutes: IntProperty(
        name="分钟",
        description="检查更新的间隔分钟",
        default=0,
        min=0,
        max=59,
    ) # type: ignore

    def draw(self, context):
        layout = self.layout
        print(addon_updater_ops.get_user_preferences(context))
        # Works best if a column, or even just self.layout.
        mainrow = layout.row()
        _ = mainrow.column()
        # Updater draw function, could also pass in col as third arg.
        addon_updater_ops.update_settings_ui(self, context)

        # Alternate draw function, which is more condensed and can be
        # placed within an existing draw function. Only contains:
        #   1) check for update/update now buttons
        #   2) toggle for auto-check (interval will be equal to what is set above)
        # addon_updater_ops.update_settings_ui_condensed(self, context, col)

        # Adding another column to help show the above condensed ui as one column
        # col = mainrow.column()
        # col.scale_y = 2
        # ops = col.operator("wm.url_open","Open webpage ")
        # ops.url=addon_updater_ops.updater.website
