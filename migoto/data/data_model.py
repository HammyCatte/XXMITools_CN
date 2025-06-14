import time
from typing import Callable, Optional, Union
import copy
import numpy
from bpy.types import Collection, Context, Mesh, Object
from numpy.typing import NDArray

from .byte_buffer import (
    AbstractSemantic,
    BufferLayout,
    NumpyBuffer,
    Semantic,
    BufferSemantic,
)
from .data_extractor import BlenderDataExtractor
from .data_importer import BlenderDataImporter
from .dxgi_format import DXGIFormat
from ..datahandling import Fatal
from ..datastructures import GameEnum


class DataModel(object):
    flip_winding: bool = False
    flip_normal: bool = False
    flip_tangent: bool = False
    flip_bitangent_sign: bool = False
    flip_texcoord_v: bool = False

    data_extractor: BlenderDataExtractor = BlenderDataExtractor()
    buffers_format: dict[str, BufferLayout] = {}
    semantic_converters: dict[AbstractSemantic, list[Callable]] = {}
    format_converters: dict[AbstractSemantic, list[Callable]] = {}

    blender_data_formats: dict[Semantic, DXGIFormat] = {
        Semantic.Index: DXGIFormat.R32_UINT,
        Semantic.VertexId: DXGIFormat.R32_UINT,
        Semantic.Normal: DXGIFormat.R16G16B16_FLOAT,
        Semantic.Tangent: DXGIFormat.R16G16B16_FLOAT,
        Semantic.BitangentSign: DXGIFormat.R16_FLOAT,
        Semantic.Color: DXGIFormat.R32G32B32A32_FLOAT,
        Semantic.TexCoord: DXGIFormat.R32G32_FLOAT,
        Semantic.Position: DXGIFormat.R32G32B32_FLOAT,
        Semantic.Blendindices: DXGIFormat.R32_UINT,
        Semantic.Blendweight: DXGIFormat.R32_FLOAT,
        Semantic.ShapeKey: DXGIFormat.R32G32B32_FLOAT,
    }

    def set_data(
        self,
        obj: Object,
        mesh: Mesh,
        index_buffer: NumpyBuffer,
        vertex_buffer: NumpyBuffer,
        vg_remap: Optional[NDArray],
        mirror_mesh: bool = False,
    ) -> None:
        # Copy default converters
        semantic_converters, format_converters = {}, {}
        semantic_converters.update(copy.deepcopy(self.semantic_converters))
        format_converters.update(copy.deepcopy(self.format_converters))

        # Add generic converters

        # Swap first and third index for each triangle in index buffer
        flip_winding: bool = (
            self.flip_winding if not mirror_mesh else not self.flip_winding
        )
        if flip_winding:
            self._insert_converter(
                semantic_converters,
                AbstractSemantic(Semantic.Index),
                self.converter_rgb_to_bgr_vector,
            )

        for semantic in vertex_buffer.layout.semantics:
            # Skip tangents import, we'll recalc them on export
            if semantic.abstract.enum in [Semantic.Tangent, Semantic.BitangentSign]:
                continue
            # Invert X coord of every vector in arrays required to mirror mesh
            if mirror_mesh and semantic.abstract.enum in [
                Semantic.Position,
                Semantic.ShapeKey,
                Semantic.Normal,
            ]:
                self._insert_converter(
                    semantic_converters, semantic.abstract, self.converter_mirror_vector
                )
            # Flip V component of UV maps
            if self.flip_texcoord_v and semantic.abstract.enum == Semantic.TexCoord:
                self._insert_converter(
                    semantic_converters,
                    semantic.abstract,
                    self.converter_flip_texcoord_v,
                )
            # Flip normals
            if self.flip_normal and semantic.abstract.enum == Semantic.Normal:
                self._insert_converter(
                    semantic_converters, semantic.abstract, self.converter_flip_vector
                )
            # Remap indicies of VG groups
            if vg_remap is not None:
                if semantic.abstract.enum == Semantic.Blendindices:
                    self._insert_converter(
                        semantic_converters,
                        semantic.abstract,
                        lambda data: vg_remap[data],
                    )
            # Auto-resize second dimension of data array to match Blender format
            if semantic.abstract.enum not in [
                Semantic.Blendindices,
                Semantic.Blendweight,
            ]:
                blender_num_values = self.blender_data_formats[
                    semantic.abstract.enum
                ].get_num_values()
                if semantic.get_num_values() != blender_num_values:
                    converter = (
                        lambda data,
                        width=blender_num_values: self.converter_resize_second_dim(
                            data, width
                        )
                    )
                    self._insert_converter(
                        format_converters, semantic.abstract, converter
                    )

        data_importer: BlenderDataImporter = BlenderDataImporter()

        data_importer.set_data(
            obj,
            mesh,
            index_buffer,
            vertex_buffer,
            semantic_converters,
            format_converters,
        )

    def get_data(
        self,
        context: Context,
        collection: Collection,
        obj: Object,
        mesh: Mesh,
        excluded_buffers: list[str],
        mirror_mesh: bool = False,
    ) -> tuple[dict[str, NumpyBuffer], int]:
        try:
            index_data, vertex_buffer = self.export_data(
                context, collection, mesh, excluded_buffers, mirror_mesh
            )
        except RuntimeError:
            raise Fatal(
                f"Failed to calculate tangents! Ensure the mesh({obj.name}) has at least 1 UV map called 'TEXCOORD.xy'"
            )
        buffers = self.build_buffers(index_data, vertex_buffer, excluded_buffers)
        return buffers, len(vertex_buffer)

    def build_buffers(
        self, index_data, vertex_buffer, excluded_buffers
    ) -> dict[str, NumpyBuffer]:
        start_time = time.time()

        result = {}
        for buffer_name, buffer_layout in self.buffers_format.items():
            buffer = None
            if buffer_name in excluded_buffers:
                continue
            for semantic in buffer_layout.semantics:
                if semantic.abstract.enum == Semantic.ShapeKey:
                    continue
                if semantic.abstract.enum == Semantic.Index:
                    data = index_data
                else:
                    data = vertex_buffer.get_field(semantic.get_name())
                if buffer is None:
                    buffer = NumpyBuffer(buffer_layout, size=len(data))
                buffer.import_semantic_data(data, semantic)
            if buffer is None:
                continue
            result[buffer_name] = buffer

        print(
            f"Buffers build time: {time.time() - start_time:.3f}s ({len(result)} buffers)"
        )

        return result

    def export_data(
        self, context, collection, mesh, excluded_buffers, mirror_mesh: bool = False
    ) -> tuple[NDArray, NumpyBuffer]:
        export_layout, fetch_loop_data = self.make_export_layout(excluded_buffers)
        index_data, vertex_buffer = self.get_mesh_data(
            context, collection, mesh, export_layout, fetch_loop_data, mirror_mesh
        )
        return index_data, vertex_buffer

    def make_export_layout(self, excluded_buffers) -> tuple[BufferLayout, bool]:
        fetch_loop_data = False

        if len(excluded_buffers) == 0:
            fetch_loop_data = True
        else:
            for buffer_name, buffer_layout in self.buffers_format.items():
                if buffer_name not in excluded_buffers:
                    for semantic in buffer_layout.semantics:
                        if (
                            semantic.abstract.enum
                            in self.data_extractor.blender_loop_semantics
                        ):
                            fetch_loop_data = True
                            break

        export_layout = BufferLayout([])
        for buffer_name, buffer_layout in self.buffers_format.items():
            exclude_buffer = buffer_name in excluded_buffers
            for semantic in buffer_layout.semantics:
                if (
                    exclude_buffer
                    and semantic.abstract.enum
                    not in self.data_extractor.blender_loop_semantics
                ):
                    continue
                if semantic.abstract.enum == Semantic.ShapeKey:
                    continue
                export_layout.add_element(semantic)

        return export_layout, fetch_loop_data

    def get_mesh_data(
        self,
        context: Context,
        collection: Collection,
        mesh: Mesh,
        export_layout: BufferLayout,
        fetch_loop_data: bool,
        mirror_mesh: bool = False,
    ) -> tuple[NDArray, NumpyBuffer]:
        # vertex_ids_cache, cache_vertex_ids = None, False
        vertex_ids_cache = None

        flip_winding = self.flip_winding if not mirror_mesh else not self.flip_winding
        flip_bitangent_sign = (
            self.flip_bitangent_sign
            if not mirror_mesh
            else not self.flip_bitangent_sign
        )

        # if not fetch_loop_data:
        #     if (
        #         collection
        #         != context.scene.wwmi_tools_settings.vertex_ids_cached_collection
        #     ):
        #         # Cache contains data for different object and must be cleared
        #         context.scene.wwmi_tools_settings.vertex_ids_cache = ""
        #         fetch_loop_data = True
        #         cache_vertex_ids = True
        #     else:
        #         # Partial export is enabled
        #         if context.scene.wwmi_tools_settings.vertex_ids_cache:
        #             # Valid vertex ids cache exists, lets load it
        #             vertex_ids_cache = numpy.array(
        #                 json.loads(context.scene.wwmi_tools_settings.vertex_ids_cache)
        #             )
        #         else:
        #             # Cache is clear, we'll have to fetch loop data once
        #             fetch_loop_data = True
        #             cache_vertex_ids = True
        # elif context.scene.wwmi_tools_settings.vertex_ids_cache:
        #     # We're going to fetch loop data, cache must be cleared
        #     context.scene.wwmi_tools_settings.vertex_ids_cache = ""

        # Copy default converters
        semantic_converters, format_converters = {}, {}
        semantic_converters.update(copy.deepcopy(self.semantic_converters))
        format_converters.update(copy.deepcopy(self.format_converters))

        # Add generic converters
        for semantic in export_layout.semantics:
            # Flip normals
            if self.flip_normal and semantic.abstract.enum == Semantic.Normal:
                self._insert_converter(
                    semantic_converters, semantic.abstract, self.converter_flip_vector
                )
            # Flip tangents
            if self.flip_tangent and semantic.abstract.enum == Semantic.Tangent:
                self._insert_converter(
                    semantic_converters, semantic.abstract, self.converter_flip_vector
                )
            # Flip bitangent sign
            if flip_bitangent_sign and semantic.abstract.enum == Semantic.BitangentSign:
                self._insert_converter(
                    semantic_converters, semantic.abstract, self.converter_flip_vector
                )
            # Invert X coord of every vector in arrays required to mirror mesh
            if mirror_mesh and semantic.abstract.enum in [
                Semantic.Position,
                Semantic.Normal,
                Semantic.Tangent,
            ]:
                self._insert_converter(
                    semantic_converters, semantic.abstract, self.converter_mirror_vector
                )
            # Flip V component of UV maps
            if self.flip_texcoord_v and semantic.abstract.enum == Semantic.TexCoord:
                self._insert_converter(
                    semantic_converters,
                    semantic.abstract,
                    self.converter_flip_texcoord_v,
                )

        # If vertex_ids_cache is *not* None, get_data method will skip loop data fetching
        index_buffer, vertex_buffer = self.data_extractor.get_data(
            mesh,
            export_layout,
            self.blender_data_formats,
            semantic_converters,
            format_converters,
            vertex_ids_cache,
            flip_winding=flip_winding,
        )

        # if cache_vertex_ids:
        #     # As vertex_ids_cache is None, get_data fetched loop data for us and we can cache vertex ids
        #     vertex_ids = vertex_buffer.get_field(
        #         AbstractSemantic(Semantic.VertexId).get_name()
        #     )
        #     context.scene.wwmi_tools_settings.vertex_ids_cache = json.dumps(
        #         vertex_ids.tolist()
        #     )
        #     context.scene.wwmi_tools_settings.vertex_ids_cached_collection = collection

        return index_buffer, vertex_buffer

    @staticmethod
    def converter_flip_vector(data: NDArray) -> NDArray:
        return -data

    @staticmethod
    def converter_mirror_vector(data: NDArray) -> NDArray:
        data[:, 0] *= -1
        return data

    @staticmethod
    def converter_flip_texcoord_v(data: NDArray) -> NDArray:
        if data.dtype != numpy.float32:
            data = data.astype(numpy.float32)
        data[:, 1] = 1.0 - data[:, 1]
        return data

    @staticmethod
    def converter_reshape_second_dim(data: NDArray, width: int) -> NDArray:
        """
        Restructures 2-dim numpy array's 2-nd dimension to given width by regrouping values
        Automatically converts 1-dim array to 2-dim with given width (every `width` elements are getting wrapped in array)
        """
        data = numpy.reshape(data, (-1, width))
        return data

    @staticmethod
    def converter_resize_second_dim(
        data: NDArray, width: int, fill: Union[int, float] = 0
    ) -> NDArray:
        """
        Restructures 2-dim numpy array's 2-nd dimension to given width by padding or dropping values
        Automatically converts 1-dim array to 2-dim with given width (every element is getting padded to width)
        """
        num_dimensions, num_values = data.ndim, data.shape[1] if data.ndim > 1 else 0
        if num_dimensions != 2 or num_values != width:
            if num_values < width:
                if num_dimensions == 1:
                    # Array is 1-dim one and requires conversion to 2-dim
                    num_values = 1
                    # Wrap every value into array
                    data = data.reshape(-1, 1)
                    if width == 1:
                        # Requested width is also 1, lets exit early
                        return data
                # Change the size of 2-nd dimension
                new_shape = list(data.shape)
                new_shape[1] = width
                if fill == 1:
                    new_data = numpy.ones(dtype=data.dtype, shape=new_shape)
                else:
                    new_data = numpy.zeros(dtype=data.dtype, shape=new_shape)
                    if fill != 0:
                        new_data.fill(fill)
                # Fill empty array with data
                new_data[:, 0:num_values] = data
                return new_data
            else:
                # Trim excessive values to given width
                return data[:, : -(num_values - width)]
        else:
            # Array structure
            return data

    @staticmethod
    def converter_rgb_to_bgr_vector(data: NDArray) -> NDArray:
        data = data.flatten()
        # Create array from 0 to len
        # Creates [0, 1, 2, 3, 4, 5] for len=6
        indices = numpy.arange(len(data))
        # Convert flat array to 2-dim array of index triads
        # [0, 1, 2, 3, 4, 5] -> [[0, 1, 2], [3, 4, 5]]
        indices = indices.reshape(-1, 3)
        # Swap every first with every third element of index triads
        # [[0, 1, 2], [3, 4, 5]] -> [[2, 1, 0], [5, 4, 3]]
        indices[:, [0, 2]] = indices[:, [2, 0]]
        # Destroy first dimension so we could use the array as index for loop data array
        # [[2, 1, 0], [5, 4, 3]] -> [2, 1, 0, 5, 4, 3]
        indices = indices.flatten()
        # Swap every first with every third element of loop data array
        data = data[indices]

        data = data.reshape(-1, 3)

        return data

    @staticmethod
    def _insert_converter(
        converters, abstract_semantic: AbstractSemantic, converter: Callable
    ) -> None:
        if abstract_semantic not in converters.keys():
            converters[abstract_semantic] = []
        converters[abstract_semantic].insert(0, converter)


class DataModelXXMI(DataModel):
    game: GameEnum
    flip_texcoords_vertical: dict[str, bool]
    buffers_format: dict[str, BufferLayout]
    format_converters: dict[AbstractSemantic, list[Callable]]
    semantic_converters: dict[AbstractSemantic, list[Callable]]
    mirror_mesh: bool = False
    flip_winding: bool = False
    flip_normal: bool = False
    flip_tangent: bool = False
    flip_bitangent_sign: bool = False
    normalize_weights: bool = False

    @classmethod
    def from_obj(
        cls,
        obj: Object,
        game: GameEnum,
        normalize_weights: bool = False,
        is_posed_mesh: bool = False,
    ) -> "DataModelXXMI":
        cls = super().__new__(cls)
        cls.format_converters = {}
        cls.semantic_converters = {}
        cls.flip_texcoords_vertical = {}
        cls.buffers_format = {}
        cls.game = game
        cls.normalize_weights = normalize_weights
        for prop in [
            "3DMigoto:FlipNormal",
            "3DMigoto:FlipTangent",
            "3DMigoto:FlipWinding",
            "3DMigoto:FlipMesh",
        ]:
            if prop not in obj:
                obj[prop] = False
        cls.flip_winding = obj.get("3DMigoto:FlipWinding")
        cls.flip_normal = obj.get("3DMigoto:FlipNormal")
        cls.flip_tangent = obj.get("3DMigoto:FlipTangent")
        cls.flip_bitangent_sign = obj.get("3DMigoto:Tangent")
        cls.mirror_mesh = obj.get("3DMigoto:FlipMesh")
        if obj.get("3DMigoto:VBLayout") is None:
            raise Fatal(
                f"Object({obj.name}) is missing custom properties required for export! Reimport the mesh from dump folder."
            )
        if (ib_f := obj.get("3DMigoto:IBFormat")) is None:
            raise Fatal("Export doesn't support meshes without index buffer")
        for uv_layer in obj.data.uv_layers:
            if obj.get("3DMigoto:" + uv_layer.name) is None:
                continue
            cls.flip_texcoords_vertical[uv_layer.name] = obj[
                "3DMigoto:" + uv_layer.name
            ]["flip_v"]
        ib_format: DXGIFormat = DXGIFormat(ib_f)
        if ib_format.dxgi_type == DXGIFormat.R16_UINT.dxgi_type:
            # 16-bit index buffer promoted to 32-bit
            ib_format = DXGIFormat.from_type(
                DXGIFormat.R32_UINT.dxgi_type,
                ib_format.get_num_values(),
            )
        cls.buffers_format: dict[str, BufferLayout] = {
            "IB": BufferLayout(
                [
                    BufferSemantic(
                        AbstractSemantic(Semantic.Index),
                        ib_format,
                    )
                ]
            ),
            "Position": BufferLayout([]),
            "Blend": BufferLayout([]),
            "TexCoord": BufferLayout([]),
        }
        pos_semantics: list[Semantic] = [
            Semantic.Position,
            Semantic.Normal,
            Semantic.Tangent,
        ]
        blend_semantics: list[Semantic] = [Semantic.Blendweight, Semantic.Blendindices]
        tex_semantics: list[Semantic] = [Semantic.TexCoord, Semantic.Color]
        if game == GameEnum.HonkaiImpactPart2:
            pos_semantics = [
                Semantic.Position,
                Semantic.Normal,
                Semantic.Tangent,
                Semantic.Color,
            ]
            tex_semantics = [Semantic.TexCoord]
        if not is_posed_mesh:
            pos_semantics: list[Semantic] = [
                Semantic.Position,
                Semantic.Normal,
                Semantic.Tangent,
                Semantic.Blendweight,
                Semantic.Blendindices,
                Semantic.TexCoord,
                Semantic.Color,
            ]
            blend_semantics: list[Semantic] = []
            tex_semantics: list[Semantic] = []
        try:
            for entry in obj.get("3DMigoto:VBLayout"):
                s_dict = entry.to_dict()
                if s_dict["SemanticName"] == "BLENDWEIGHTS":
                    s_dict["SemanticName"] = "BLENDWEIGHT"
                new_semantic = BufferSemantic(
                    # offset=semantic_dict["AlignedByteOffset"],
                    # semantic_dict["InputSlotClass"],
                    # stride = 0,
                    abstract=AbstractSemantic(
                        Semantic(s_dict["SemanticName"]),
                        s_dict["SemanticIndex"],
                    ),
                    format=DXGIFormat(s_dict["Format"]),
                    input_slot=s_dict["InputSlot"],
                    data_step_rate=s_dict["InstanceDataStepRate"],
                    remapped_abstract=AbstractSemantic(
                        Semantic(
                            s_dict.get(
                                "RemappedSemanticName",
                                s_dict["SemanticName"],
                            )
                        ),
                        s_dict.get("RemappedSemanticIndex", s_dict["SemanticIndex"]),
                    ),
                )
                if new_semantic.abstract.enum in pos_semantics:
                    if (
                        new_semantic.abstract.enum
                        in [Semantic.Normal, Semantic.Position]
                        and new_semantic.get_num_values() == 4
                    ):
                        cls.semantic_converters[new_semantic.abstract] = [
                            lambda data: cls.converter_resize_second_dim(
                                data, 4, fill=1
                            )
                        ]
                    if (
                        new_semantic.abstract.enum == Semantic.Tangent
                        and new_semantic.get_num_values() == 4
                    ):
                        # Tangent is 4D vector, we need to convert it to 3D, 1D BitangentSign
                        cls.buffers_format["Position"].add_element(
                            BufferSemantic(
                                new_semantic.abstract,
                                DXGIFormat.from_type(new_semantic.format.dxgi_type, 3),
                                new_semantic.input_slot,
                                new_semantic.data_step_rate,
                                remapped_abstract=new_semantic.remapped_abstract,
                            )
                        )
                        cls.buffers_format["Position"].add_element(
                            BufferSemantic(
                                AbstractSemantic(Semantic.BitangentSign),
                                DXGIFormat.from_type(new_semantic.format.dxgi_type, 1),
                                new_semantic.input_slot,
                                new_semantic.data_step_rate,
                                remapped_abstract=new_semantic.remapped_abstract,
                            )
                        )
                        continue
                    cls.buffers_format["Position"].add_element(new_semantic)
                elif new_semantic.abstract.enum in blend_semantics:
                    if (
                        new_semantic.abstract.enum is Semantic.Blendweight
                        and cls.normalize_weights
                    ):
                        cls.format_converters[new_semantic.abstract] = [
                            lambda data: cls.converter_normalize_weights(data)
                        ]
                    cls.buffers_format["Blend"].add_element(new_semantic)
                elif new_semantic.abstract.enum in tex_semantics:
                    cls.buffers_format["TexCoord"].add_element(new_semantic)
        except KeyError:
            raise Fatal(
                f"Object({obj.name}) doesn't count with the custom properties required for export! Reimport the mesh from dump folder."
            )
        if cls.game == GameEnum.ZenlessZoneZero:
            bitan_abstract: AbstractSemantic = AbstractSemantic(Semantic.BitangentSign)
            if cls.buffers_format["Position"].get_element(bitan_abstract) is not None:
                cls.format_converters[bitan_abstract] = [
                    lambda data: cls.converter_flip_bitangent_sign(data)
                ]
        return cls

    def converter_normalize_weights(self, data: NDArray) -> NDArray:
        """Normalizes weight values to ensure they sum to 1.0 for each vertex"""
        if data.size == data.shape[0]:
            return data
        sums: NDArray = numpy.sum(data, axis=1, keepdims=True)
        # Avoid division by zero - if sum is 0, set it to 1
        sums[sums == 0] = 1.0
        normalized: NDArray = data / sums

        return normalized

    def converter_flip_bitangent_sign(self, data: NDArray) -> NDArray:
        """Flips the sign of the bitangent vector"""
        data *= -1
        return data

    def get_mesh_data(
        self,
        context: Context,
        collection: Collection,
        mesh: Mesh,
        export_layout: BufferLayout,
        fetch_loop_data: bool,
        mirror_mesh: bool = False,
    ) -> tuple[NDArray, NumpyBuffer]:
        flip_winding: bool = (
            self.flip_winding if not self.mirror_mesh else not self.flip_winding
        )
        flip_bitangent_sign: bool = (
            self.flip_bitangent_sign
            if not self.mirror_mesh
            else not self.flip_bitangent_sign
        )

        # Copy default converters
        semantic_converters: dict[AbstractSemantic, list[Callable]] = {}
        format_converters: dict[AbstractSemantic, list[Callable]] = {}
        semantic_converters.update(copy.deepcopy(self.semantic_converters))
        format_converters.update(copy.deepcopy(self.format_converters))

        # Add generic converters
        for semantic in export_layout.semantics:
            # Flip normals
            if self.flip_normal and semantic.abstract.enum == Semantic.Normal:
                self._insert_converter(
                    semantic_converters, semantic.abstract, self.converter_flip_vector
                )
            # Flip tangents
            if self.flip_tangent and semantic.abstract.enum == Semantic.Tangent:
                self._insert_converter(
                    semantic_converters, semantic.abstract, self.converter_flip_vector
                )
            # Flip bitangent sign
            if flip_bitangent_sign and semantic.abstract.enum == Semantic.BitangentSign:
                self._insert_converter(
                    semantic_converters, semantic.abstract, self.converter_flip_vector
                )
            # Invert X coord of every vector in arrays required to mirror mesh
            if self.mirror_mesh and semantic.abstract.enum in [
                Semantic.Position,
                Semantic.Normal,
                Semantic.Tangent,
            ]:
                self._insert_converter(
                    semantic_converters, semantic.abstract, self.converter_mirror_vector
                )
            # Flip V component of UV maps
            if self.flip_texcoord_v and semantic.abstract.enum == Semantic.TexCoord:
                self._insert_converter(
                    semantic_converters,
                    semantic.abstract,
                    self.converter_flip_texcoord_v,
                )
            if (
                self.flip_texcoords_vertical.get(semantic.get_name())
                and semantic.abstract.enum == Semantic.TexCoord
            ):
                self._insert_converter(
                    semantic_converters,
                    semantic.abstract,
                    self.converter_flip_texcoord_v,
                )

        index_buffer, vertex_buffer = self.data_extractor.get_data(
            mesh,
            export_layout,
            self.blender_data_formats,
            semantic_converters,
            format_converters,
            flip_winding=flip_winding,
        )
        return index_buffer, vertex_buffer
