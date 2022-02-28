from __future__ import annotations

from itertools import chain
from operator import attrgetter, itemgetter
import sys
from typing import Iterator, Iterable, NamedTuple, TYPE_CHECKING

import rich.repr
from rich.console import Console, ConsoleOptions, RenderResult
from rich.control import Control
from rich.segment import Segment, SegmentLines
from rich.style import Style

from . import log
from .geometry import Region, Offset, Size

from .layout import WidgetPlacement
from ._loop import loop_last
from ._types import Lines
from .widget import Widget

if sys.version_info >= (3, 10):
    from typing import TypeAlias
else:  # pragma: no cover
    from typing_extensions import TypeAlias


if TYPE_CHECKING:
    from .widget import Widget


class NoWidget(Exception):
    """Raised when there is no widget at the requested coordinate."""


class ReflowResult(NamedTuple):
    """The result of a reflow operation. Describes the chances to widgets."""

    hidden: set[Widget]
    shown: set[Widget]
    resized: set[Widget]


class RenderRegion(NamedTuple):
    """Defines the absolute location of a Widget."""

    region: Region
    order: tuple[int, ...]
    clip: Region


RenderRegionMap: TypeAlias = dict[Widget, RenderRegion]


@rich.repr.auto
class LayoutUpdate:
    """A renderable containing the result of a render for a given region."""

    def __init__(self, lines: Lines, region: Region) -> None:
        self.lines = lines
        self.region = region

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield Control.home()
        x = self.region.x
        new_line = Segment.line()
        move_to = Control.move_to
        for last, (y, line) in loop_last(enumerate(self.lines, self.region.y)):
            yield move_to(x, y)
            yield from line
            if not last:
                yield new_line

    def __rich_repr__(self) -> rich.repr.Result:
        x, y, width, height = self.region
        yield "x", x
        yield "y", y
        yield "width", width
        yield "height", height


@rich.repr.auto(angular=True)
class Compositor:
    """Responsible for storing information regarding the relative positions of Widgets and rendering them."""

    def __init__(self) -> None:
        # A mapping of Widget on to its "render location" (absolute position / depth)
        self.map: RenderRegionMap = {}

        # All widgets considered in the arrangement
        # Not this may be a supperset of self.map.keys() as some widgets may be invisible for various reasons
        self.widgets: set[Widget] = set()

        # Dimensions of the arrangement
        self.width = 0
        self.height = 0

        self.regions: dict[Widget, tuple[Region, Region]] = {}
        self._cuts: list[list[int]] | None = None
        self._require_update: bool = True
        self.background = ""

    def __rich_repr__(self) -> rich.repr.Result:
        yield "width", self.width
        yield "height", self.height
        yield "widgets", self.widgets

    def check_update(self) -> bool:
        return self._require_update

    def require_update(self) -> None:
        self._require_update = True
        self.reset()
        self.map.clear()
        self.widgets.clear()

    def reset_update(self) -> None:
        self._require_update = False

    def reset(self) -> None:
        self._cuts = None

    def reflow(self, parent: Widget, size: Size) -> ReflowResult:
        """Reflow (layout) widget and its children.

        Args:
            parent (Widget): The root widget.
            size (Size): Size of the area to be filled.

        Returns:
            ReflowResult: Hidden shown and resized widgets
        """
        self.reset()

        self.width = size.width
        self.height = size.height

        map, virtual_size = self._arrange_root(parent)
        log(map)
        self._require_update = False

        old_widgets = set(self.map.keys())
        new_widgets = set(map.keys())
        # Newly visible widgets
        shown_widgets = new_widgets - old_widgets
        # Newly hidden widgets
        hidden_widgets = old_widgets - new_widgets

        self.map.clear()
        self.map.update(map)

        # Copy renders if the size hasn't changed
        new_renders = {
            widget: (region, clip) for widget, (region, _order, clip) in map.items()
        }
        self.regions = new_renders

        # Widgets with changed size
        resized_widgets = {
            widget
            for widget, (region, *_) in map.items()
            if widget in old_widgets and widget.size != region.size
        }

        parent.virtual_size = virtual_size

        return ReflowResult(
            hidden=hidden_widgets, shown=shown_widgets, resized=resized_widgets
        )

    def _arrange_root(self, root: Widget) -> tuple[RenderRegionMap, Size]:
        """Arrange a widgets children based on its layout attribute.

        Args:
            root (Widget): Top level widget.

        Returns:
            map[dict[Widget, RenderRegion], Size]: A mapping of widget on to render region
                and the "virtual size" (scrollable reason)
        """
        size = Size(self.width, self.height)
        ORIGIN = Offset(0, 0)

        map: dict[Widget, RenderRegion] = {}
        widgets: set[Widget] = set()

        def add_widget(
            widget,
            region: Region,
            order: tuple[int, ...],
            clip: Region,
        ):
            widgets: set[Widget] = set()
            styles_offset = widget.styles.offset
            total_region = region
            layout_offset = (
                styles_offset.resolve(region.size, clip.size)
                if styles_offset
                else ORIGIN
            )

            map[widget] = RenderRegion(region + layout_offset, order, clip)

            if widget.layout is not None:
                scroll = widget.scroll
                total_region = region.size.region
                sub_clip = clip.intersection(region)

                placements, arranged_widgets = widget.layout.arrange(
                    widget, region.size, scroll
                )
                widgets.update(arranged_widgets)
                placements = [placement.apply_margin() for placement in placements]
                placements.sort(key=attrgetter("order"))

                for sub_region, sub_widget, z in placements:
                    total_region = total_region.union(sub_region)
                    if sub_widget is not None:
                        add_widget(
                            sub_widget,
                            sub_region + region.origin - scroll,
                            sub_widget.z + (z,),
                            sub_clip,
                        )
            return total_region.size

        virtual_size = add_widget(root, size.region, (), size.region)
        self.widgets.clear()
        self.widgets.update(widgets)
        return map, virtual_size

    async def mount_all(self, view: "View") -> None:
        view.mount(*self.widgets)

    def __iter__(self) -> Iterator[tuple[Widget, Region, Region]]:
        layers = sorted(self.map.items(), key=lambda item: item[1].order, reverse=True)
        for widget, (region, order, clip) in layers:
            yield widget, region.intersection(clip), region

    def get_offset(self, widget: Widget) -> Offset:
        """Get the offset of a widget."""
        try:
            return self.map[widget].region.origin
        except KeyError:
            raise NoWidget("Widget is not in layout")

    def get_widget_at(self, x: int, y: int) -> tuple[Widget, Region]:
        """Get the widget under the given point or None."""
        for widget, cropped_region, region in self:
            if widget.is_visual and cropped_region.contains(x, y):
                return widget, region
        raise NoWidget(f"No widget under screen coordinate ({x}, {y})")

    def get_style_at(self, x: int, y: int) -> Style:
        """Get the Style at the given cell or Style.null()

        Args:
            x (int): X position within the Layout
            y (int): Y position within the Layout

        Returns:
            Style: The Style at the cell (x, y) within the Layout
        """
        try:
            widget, region = self.get_widget_at(x, y)
        except NoWidget:
            return Style.null()
        if widget not in self.regions:
            return Style.null()
        lines = widget._get_lines()
        x -= region.x
        y -= region.y
        line = lines[y]
        end = 0
        for segment in line:
            end += segment.cell_length
            if x < end:
                return segment.style or Style.null()
        return Style.null()

    def get_widget_region(self, widget: Widget) -> Region:
        """Get the Region of a Widget contained in this Layout.

        Args:
            widget (Widget): The Widget in this layout you wish to know the Region of.

        Raises:
            NoWidget: If the Widget is not contained in this Layout.

        Returns:
            Region: The Region of the Widget.

        """
        try:
            region, *_ = self.map[widget]
        except KeyError:
            raise NoWidget("Widget is not in layout")
        else:
            return region

    @property
    def cuts(self) -> list[list[int]]:
        """Get vertical cuts.

        A cut is every point on a line where a widget starts or ends.

        Returns:
            list[list[int]]: A list of cuts for every line.
        """
        if self._cuts is not None:
            return self._cuts
        width = self.width
        height = self.height
        screen_region = Region(0, 0, width, height)
        cuts = [[0, width] for _ in range(height)]

        for region, order, clip in self.map.values():
            region = region.intersection(clip)
            if region and (region in screen_region):
                region_cuts = (region.x, region.x + region.width)
                for cut in cuts[region.y : region.y + region.height]:
                    cut.extend(region_cuts)

        # Sort the cuts for each line
        self._cuts = [sorted(set(cut_set)) for cut_set in cuts]
        return self._cuts

    def _get_renders(self, console: Console) -> Iterable[tuple[Region, Region, Lines]]:
        _rich_traceback_guard = True

        if self.map:
            widget_regions = sorted(
                (
                    (widget, region, order, clip)
                    for widget, (region, order, clip) in self.map.items()
                ),
                key=itemgetter(2),
                reverse=True,
            )
        else:
            widget_regions = []

        for widget, region, _order, clip in widget_regions:

            if not (widget.is_visual and widget.visible):
                continue

            lines = widget._get_lines()

            if region in clip:
                yield region, clip, lines
            elif clip.overlaps(region):
                new_region = region.intersection(clip)
                delta_x = new_region.x - region.x
                delta_y = new_region.y - region.y
                splits = [delta_x, delta_x + new_region.width]
                lines = lines[delta_y : delta_y + new_region.height]
                divide = Segment.divide
                lines = [list(divide(line, splits))[1] for line in lines]
                yield region, clip, lines

    @classmethod
    def _assemble_chops(
        cls, chops: list[dict[int, list[Segment] | None]]
    ) -> Iterable[Iterable[Segment]]:

        from_iterable = chain.from_iterable
        for bucket in chops:
            yield from_iterable(
                line for _, line in sorted(bucket.items()) if line is not None
            )

    def render(
        self,
        console: Console,
        *,
        crop: Region | None = None,
    ) -> SegmentLines:
        """Render a layout.

        Args:
            console (Console): Console instance.
            clip (Optional[Region]): Region to clip to.

        Returns:
            SegmentLines: A renderable
        """
        width = self.width
        height = self.height
        screen = Region(0, 0, width, height)

        crop_region = crop.intersection(screen) if crop else screen

        _Segment = Segment
        divide = _Segment.divide

        # Maps each cut on to a list of segments
        cuts = self.cuts
        chops: list[dict[int, list[Segment] | None]] = [
            {cut: None for cut in cut_set} for cut_set in cuts
        ]

        # TODO: Provide an option to update the background
        background_style = console.get_style(self.background)
        background_render = [
            [_Segment(" " * width, background_style)] for _ in range(height)
        ]
        # Go through all the renders in reverse order and fill buckets with no render
        renders = self._get_renders(console)

        for region, clip, lines in chain(
            renders, [(screen, screen, background_render)]
        ):
            render_region = region.intersection(clip)
            for y, line in zip(render_region.y_range, lines):

                first_cut, last_cut = render_region.x_extents
                final_cuts = [cut for cut in cuts[y] if (last_cut >= cut >= first_cut)]

                if len(final_cuts) == 2:
                    cut_segments = [line]
                else:
                    render_x = render_region.x
                    relative_cuts = [cut - render_x for cut in final_cuts]
                    _, *cut_segments = divide(line, relative_cuts)
                for cut, segments in zip(final_cuts, cut_segments):
                    if chops[y][cut] is None:
                        chops[y][cut] = segments

        # Assemble the cut renders in to lists of segments
        crop_x, crop_y, crop_x2, crop_y2 = crop_region.corners
        output_lines = self._assemble_chops(chops[crop_y:crop_y2])

        def width_view(line: list[Segment]) -> list[Segment]:
            if line:
                div_lines = list(divide(line, [crop_x, crop_x2]))
                line = div_lines[1] if len(div_lines) > 1 else div_lines[0]
            return line

        if crop is not None and (crop_x, crop_x2) != (0, self.width):
            render_lines = [width_view(line) for line in output_lines]
        else:
            render_lines = list(output_lines)

        return SegmentLines(render_lines, new_lines=True)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield self.render(console)

    def update_widget(self, console: Console, widget: Widget) -> LayoutUpdate | None:
        if widget not in self.regions:
            return None

        region, clip = self.regions[widget]

        if not region.size:
            return None

        widget.clear_render_cache()

        update_region = region.intersection(clip)
        update_lines = self.render(console, crop=update_region).lines
        update = LayoutUpdate(update_lines, update_region)
        log(update)
        return update