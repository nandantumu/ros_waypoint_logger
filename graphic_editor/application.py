"""
Race Line Trajectory Editor
Author: Nandan Tumu (nandan@nandantumu.com)
License: AGPL-3.0

This application provides a graphical interface for editing race line trajectories with velocity profiles.
It allows users to:
1. Load trajectory data from CSV files
2. Edit the position and velocity of waypoints along a racing line
3. Visualize the trajectory in both position (x,y) and velocity plots
4. Save the modified trajectory to a CSV file

The editor is designed for circuit/track racing applications where the path is a closed loop.
Velocities and positions are represented as periodic cubic splines to ensure smooth transitions.

Usage:
    python application.py [path/to/trajectory.csv]

The trajectory CSV should have the following columns:
    s_m: distance along the path (meters)
    x_m: x-coordinate (meters)
    y_m: y-coordinate (meters)
    vx_mps: velocity (meters per second)

Additional columns like psi_rad (heading), kappa_radpm (curvature), and ax_mps2 (acceleration)
will be computed when saving the trajectory.
"""

import sys
import pandas as pd
import numpy as np
from scipy.interpolate import CubicSpline, interp1d

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QSlider,
    QLabel,
)
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QPainterPath

from waypoints_loader import load_waypoints


class MultiColorLine(pg.GraphicsObject):
    """A graphics object that displays a multi-colored line with colors determined by values."""

    def __init__(self, x, y, values, colormap, width=1, connect="all"):
        """
        Parameters:
        -----------
        x, y : array-like
            Arrays of x and y coordinates
        values : array-like
            Array of values used to determine the color at each point
        colormap : pg.ColorMap
            PyQtGraph colormap used to map values to colors
        width : int, optional
            Line width
        connect : str or array, optional
            Same meaning as for pg.PlotDataItem
        """
        pg.GraphicsObject.__init__(self)
        self.x = np.asarray(x)
        self.y = np.asarray(y)
        self.values = np.asarray(values)
        self.colormap = colormap
        self.width = width

        # Determine connection points
        if connect == "all":
            self.connect_array = np.ones(len(self.x) - 1, dtype=bool)
        elif connect == "pairs":
            self.connect_array = np.zeros(len(self.x) - 1, dtype=bool)
            self.connect_array[::2] = True
        elif connect == "finite":
            self.connect_array = (
                np.isfinite(self.x[:-1])
                & np.isfinite(self.x[1:])
                & np.isfinite(self.y[:-1])
                & np.isfinite(self.y[1:])
            )
        else:
            self.connect_array = connect

        self.path = None
        self.generatePath()

    def generatePath(self):
        self.path = QPainterPath()

        # Create path
        for i in range(len(self.x) - 1):
            if self.connect_array[i]:
                # Add line segment
                self.path.moveTo(self.x[i], self.y[i])
                self.path.lineTo(self.x[i + 1], self.y[i + 1])

    def boundingRect(self):
        if self.path is None:
            return pg.QtCore.QRectF()
        return self.path.boundingRect()

    def paint(self, painter, option, widget):
        if self.path is None:
            return

        painter.setRenderHint(painter.RenderHint.Antialiasing)

        # Find min/max for color scaling
        v_min = np.min(self.values)
        v_max = np.max(self.values)
        v_range = max(0.1, v_max - v_min)  # Avoid division by zero

        # Draw each segment with a color based on its value
        for i in range(len(self.x) - 1):
            if self.connect_array[i]:
                # Calculate color
                norm_v = (self.values[i] - v_min) / v_range
                qcolor = self.colormap.mapToQColor(norm_v)

                # Set pen color
                pen = pg.mkPen(color=qcolor, width=self.width)
                painter.setPen(pen)

                # Draw line segment
                painter.drawLine(
                    QPointF(self.x[i], self.y[i]), QPointF(self.x[i + 1], self.y[i + 1])
                )

    def setData(self, x, y, values):
        """Update the data in the line."""
        self.x = np.asarray(x)
        self.y = np.asarray(y)
        self.values = np.asarray(values)

        # Recompute connect_array if length changed
        if len(self.connect_array) != len(self.x) - 1:
            self.connect_array = np.ones(len(self.x) - 1, dtype=bool)

        self.generatePath()
        self.prepareGeometryChange()
        self.update()


def spline_sample_closed(control_pts, num_points, cs_vel, s_max):
    """
    Sample a closed-loop path with velocity profile using cubic splines.

    This function creates a smooth periodic spline through the control points and
    samples it to create a path with associated velocities. It ensures the path
    is properly closed (the last point connects to the first) and handles edge cases.

    Parameters:
    -----------
    control_pts : list of tuples or numpy array
        Control points for the path as (x, y) coordinates
    num_points : int
        Number of points to sample along the spline
    cs_vel : callable
        A function that returns velocity values for given s-coordinates.
        Typically a CubicSpline object or a lambda function.
    s_max : float
        Maximum s-coordinate value, used for scaling the path parameter

    Returns:
    --------
    tuple of numpy arrays (xs, ys, vs, S)
        xs, ys: x and y coordinates of the sampled path
        vs: velocities at each point
        S: cumulative arc length at each point
    """
    if not control_pts:
        return np.array([]), np.array([]), np.array([]), np.array([])

    pts_arr = np.array(control_pts)

    if pts_arr.shape[0] == 1:
        xs_sample = np.full(num_points, pts_arr[0, 0])
        ys_sample = np.full(num_points, pts_arr[0, 1])
        try:
            v_val = cs_vel(0.0)  # Velocity at s=0 for a single point path
        except Exception:
            v_val = 0.0  # Default if cs_vel fails (e.g. spline needs wider domain)
        vs_sample = np.full(
            num_points, np.maximum(0, v_val)
        )  # Clamp this single velocity value
        S_sample = np.zeros(num_points)
        # Close loop for output format consistency
        xs = np.concatenate([xs_sample, xs_sample[:1]])
        ys = np.concatenate([ys_sample, ys_sample[:1]])
        vs = np.concatenate([vs_sample, vs_sample[:1]])
        S = np.concatenate([S_sample, S_sample[:1]])  # S[-1] will be 0
        return xs, ys, vs, S

    # Prepare path points for spline (ensure closure: pts_for_spline[0] == pts_for_spline[-1])
    pts_for_spline = np.array(pts_arr)
    if not np.allclose(pts_for_spline[0], pts_for_spline[-1]):
        pts_for_spline = np.vstack([pts_for_spline, pts_for_spline[0]])

    # Compute chord lengths and parameter t for pts_for_spline
    dx_path = np.diff(pts_for_spline[:, 0])
    dy_path = np.diff(pts_for_spline[:, 1])
    dist_path = np.hypot(dx_path, dy_path)
    t_path = np.concatenate(([0], np.cumsum(dist_path)))
    total_path_length = t_path[-1]

    if (
        total_path_length < 1e-9
    ):  # All points in pts_for_spline are effectively identical
        xs_sample = np.full(num_points, pts_for_spline[0, 0])
        ys_sample = np.full(num_points, pts_for_spline[0, 1])
        try:
            v_val = cs_vel(0.0)
        except Exception:
            v_val = 0.0
        vs_sample = np.full(num_points, np.maximum(0, v_val))
        S_sample = np.zeros(num_points)
        xs = np.concatenate([xs_sample, xs_sample[:1]])
        ys = np.concatenate([ys_sample, ys_sample[:1]])
        vs = np.concatenate([vs_sample, vs_sample[:1]])
        S = np.concatenate([S_sample, S_sample[:1]])
        return xs, ys, vs, S

    t_norm_path = (
        t_path / total_path_length
    )  # Normalized parameter [0,1] for pts_for_spline

    # Compute velocities at each point in pts_for_spline using cs_vel
    # s_ctrl_path are s-values corresponding to t_norm_path, scaled by overall s_max from CSV
    s_ctrl_path = t_norm_path * s_max
    v_values_at_s_ctrl = cs_vel(s_ctrl_path)

    # Clamp these velocities to be non-negative to prevent spline undershoot into negative territory
    v_clamped_at_s_ctrl = np.maximum(0, v_values_at_s_ctrl)

    # Ensure periodicity for the clamped velocity values (v[0] == v[-1])
    v_final_for_y_ctrl = np.array(v_clamped_at_s_ctrl)  # Make a copy
    if len(v_final_for_y_ctrl) > 0:
        v_final_for_y_ctrl[-1] = v_final_for_y_ctrl[0]

    # Build joint periodic spline [x, y, v]
    # y_ctrl uses pts_for_spline (which has x[0]==x[-1], y[0]==y[-1])
    # and v_final_for_y_ctrl (which has v[0]==v[-1])
    y_ctrl = np.column_stack(
        (pts_for_spline[:, 0], pts_for_spline[:, 1], v_final_for_y_ctrl)
    )
    cs_all = CubicSpline(t_norm_path, y_ctrl, bc_type="periodic", axis=0)

    # Sample the joint spline uniformly over one period [0,1)
    ts_sample = np.linspace(0, 1, num_points, endpoint=False)
    sampled_points = cs_all(ts_sample)

    xs_period = sampled_points[:, 0]
    ys_period = sampled_points[:, 1]
    vs_period = sampled_points[:, 2]

    # Ensure final sampled velocities are non-negative
    vs_period = np.maximum(0, vs_period)

    # Close the loop for the final output arrays by appending the first sample
    xs = np.concatenate([xs_period, xs_period[:1]])
    ys = np.concatenate([ys_period, ys_period[:1]])
    vs = np.concatenate([vs_period, vs_period[:1]])

    # Compute cumulative arc-length S for the *final sampled* path
    ds_final = np.hypot(np.diff(xs), np.diff(ys))
    S = np.concatenate(([0], np.cumsum(ds_final)))

    return xs, ys, vs, S


def insert_nearest(control_pts, new_pt):
    """
    Insert a new point into the control points list at the nearest segment.

    This function finds the closest line segment in the control points path
    to the new point and inserts the new point after the start of that segment.

    Parameters:
    -----------
    control_pts : list of tuples
        List of (x, y) coordinates representing control points
    new_pt : tuple
        (x, y) coordinates of the new point to insert

    Returns:
    --------
    list
        A new list of control points with the new point inserted
    """
    pts = np.array(control_pts)
    x0, y0 = new_pt
    min_dist = float("inf")
    insert_idx = 0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            proj = np.array([x1, y1])
        else:
            t = ((x0 - x1) * dx + (y0 - y1) * dy) / (dx * dx + dy * dy)
            t = np.clip(t, 0, 1)
            proj = np.array([x1 + t * dx, y1 + t * dy])
        d = np.hypot(x0 - proj[0], y0 - proj[1])
        if d < min_dist:
            min_dist = d
            insert_idx = i + 1
    new_ctrl = control_pts.copy()
    new_ctrl.insert(insert_idx, new_pt)
    return new_ctrl


class DraggableScatter(pg.ScatterPlotItem):
    """
    Interactive scatter plot for dragging, adding, and removing control points.

    This class allows users to manipulate control points by:
    - Dragging points to new positions
    - Double-clicking on empty space to add a new point
    - Double-clicking on an existing point to remove it

    Changes to the control points automatically update the spline representation.
    """

    def __init__(self, positions, update_callback):
        """
        Initialize the draggable scatter plot.

        Parameters:
        -----------
        positions : list of tuples
            List of (x, y) coordinates for the initial scatter points
        update_callback : callable
            Function to call when points are modified, with positions as argument
        """
        super().__init__(
            pos=positions,
            data=list(range(len(positions))),
            pen=pg.mkPen("w"),
            brush=pg.mkBrush("r"),
            size=12,
        )
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.positions = list(positions)
        self.update_callback = update_callback
        self.dragIndex = None

    def mousePressEvent(self, ev):
        view_pt = self.getViewBox().mapSceneToView(ev.scenePos())
        pts = self.pointsAt(view_pt)
        if len(pts) > 0 and ev.button() == Qt.MouseButton.LeftButton:
            self.dragIndex = pts[0].data()
            ev.accept()
        else:
            ev.ignore()

    def mouseMoveEvent(self, ev):
        if self.dragIndex is None or not (ev.buttons() & Qt.MouseButton.LeftButton):
            ev.ignore()
            return
        view_pt = self.getViewBox().mapSceneToView(ev.scenePos())
        self.positions[self.dragIndex] = (view_pt.x(), view_pt.y())
        self.setData(pos=self.positions, data=list(range(len(self.positions))))
        self.update_callback(self.positions)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        self.dragIndex = None
        ev.accept()

    def mouseDoubleClickEvent(self, ev):
        view_pt = self.getViewBox().mapSceneToView(ev.scenePos())
        pts = self.pointsAt(view_pt)
        if len(pts) > 0:
            idx = pts[0].data()
            self.positions.pop(idx)
        elif ev.button() == Qt.MouseButton.LeftButton:
            new_pt = (view_pt.x(), view_pt.y())
            self.positions = insert_nearest(self.positions, new_pt)
        else:
            ev.ignore()
            return
        self.setData(pos=self.positions, data=list(range(len(self.positions))))
        self.update_callback(self.positions)
        ev.accept()


class DraggableVelocityScatter(pg.ScatterPlotItem):
    """
    Interactive scatter plot for manipulating velocity control points.

    This class allows users to drag velocity control points vertically to adjust
    velocity values at specific points along the path. It maintains the relationship
    between path distance (s-coordinate) and velocity, ensuring that:
    - Velocity values remain non-negative
    - Periodic boundary conditions are maintained when specified

    Changes to velocity points automatically update the spline representation.
    """

    def __init__(self, s_coords, v_coords, update_callback, is_periodic=True):
        """
        Initialize the draggable velocity scatter plot.

        Parameters:
        -----------
        s_coords : array-like
            Array of s-coordinates (distance along path) for the velocity control points
        v_coords : array-like
            Array of velocity values corresponding to the s-coordinates
        update_callback : callable
            Function to call when velocities are modified
        is_periodic : bool, optional
            Whether to enforce periodic boundary conditions (default: True)
        """
        self.s_coords = np.array(s_coords)
        self.v_coords = np.array(v_coords)
        self.update_callback = update_callback
        self.is_periodic = is_periodic
        super().__init__(
            x=self.s_coords,
            y=self.v_coords,
            data=list(range(len(self.s_coords))),
            pen=pg.mkPen("w"),
            brush=pg.mkBrush("r"),
            size=10,
        )
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.dragIndex = None

    def update_positions(self, s_coords, v_coords):
        self.s_coords = np.array(s_coords)
        self.v_coords = np.array(v_coords)
        if self.is_periodic and len(self.v_coords) > 1:
            self.v_coords[-1] = self.v_coords[
                0
            ]  # Ensure periodic boundary for velocity
        self.setData(
            x=self.s_coords, y=self.v_coords, data=list(range(len(self.s_coords)))
        )

    def mousePressEvent(self, ev):
        view_pt = self.getViewBox().mapSceneToView(ev.scenePos())
        pts = self.pointsAt(view_pt)
        if len(pts) > 0 and ev.button() == Qt.MouseButton.LeftButton:
            self.dragIndex = pts[0].data()
            ev.accept()
        else:
            ev.ignore()

    def mouseMoveEvent(self, ev):
        if self.dragIndex is None or not (ev.buttons() & Qt.MouseButton.LeftButton):
            ev.ignore()
            return
        view_pt = self.getViewBox().mapSceneToView(ev.scenePos())

        # Only allow dragging in y (velocity)
        new_v = max(0, view_pt.y())  # Clamp to non-negative
        self.v_coords[self.dragIndex] = new_v

        if self.is_periodic and len(self.v_coords) > 1:
            if self.dragIndex == 0:
                self.v_coords[-1] = self.v_coords[0]
            elif self.dragIndex == len(self.v_coords) - 1:
                self.v_coords[0] = self.v_coords[-1]

        self.setData(
            x=self.s_coords, y=self.v_coords, data=list(range(len(self.s_coords)))
        )
        self.update_callback(self.s_coords, self.v_coords)  # Pass both s and v
        ev.accept()

    def mouseReleaseEvent(self, ev):
        self.dragIndex = None
        ev.accept()


class MainWindow(QMainWindow):
    def __init__(self, csv_path="../traj_examples/new_loc.csv"):
        super().__init__()
        self.csv_path = csv_path
        self.setWindowTitle("Race Line Editor: Circuit Mode")
        self.num_samples = 30

        df = load_waypoints(self.csv_path)
        self.static_pts = list(df[["x_m", "y_m"]].itertuples(index=False, name=None))
        if not self.static_pts:
            self.static_pts = [(0, 0), (1, 1), (2, 0), (3, 1)]

        s_orig = df["s_m"].to_numpy()
        v_orig = df["vx_mps"].to_numpy()
        self.v_orig = v_orig  # store original velocity profile

        # Prepare v_orig for CubicSpline with periodic boundary conditions
        v_for_spline = np.copy(v_orig)
        if len(s_orig) > 1:  # This implies bc_type will be 'periodic'
            if not np.isclose(v_for_spline[0], v_for_spline[-1]):
                v_for_spline[-1] = v_for_spline[0]  # Enforce periodicity for the spline

        # cs_vel_initial is based on the original CSV, used for resetting or reference
        self.cs_vel_initial = CubicSpline(
            s_orig,
            v_for_spline,
            bc_type="periodic" if len(s_orig) > 1 else "not-a-knot",
        )
        self.s_max = s_orig[-1]

        container = QWidget()
        main_layout = QVBoxLayout(container)

        ctrl_layout = QHBoxLayout()
        save_btn = QPushButton("Save Waypoints")
        save_btn.clicked.connect(self.save_csv)
        ctrl_layout.addWidget(save_btn)
        # Button to reset velocity control points to original waypoint velocities
        reset_btn = QPushButton("Reset Velocities")
        reset_btn.clicked.connect(self.reset_velocity_profile)
        ctrl_layout.addWidget(reset_btn)
        ctrl_layout.addStretch()
        main_layout.addLayout(ctrl_layout)

        plot_layout = QHBoxLayout()
        self.plot = pg.PlotWidget()
        vb = self.plot.getViewBox()
        vb.setAspectLocked(True)
        vb.setMouseMode(pg.ViewBox.RectMode)
        plot_layout.addWidget(self.plot, stretch=1)
        slider_layout = QVBoxLayout()
        self.slider_label = QLabel(f"Points: {self.num_samples}")
        self.slider = QSlider(Qt.Orientation.Vertical)
        self.slider.setRange(1, 50)
        self.slider.setValue(self.num_samples)
        self.slider.valueChanged.connect(self.on_slider_change)
        slider_layout.addWidget(self.slider_label)
        slider_layout.addWidget(self.slider)
        slider_layout.addStretch()
        plot_layout.addLayout(slider_layout)
        main_layout.addLayout(plot_layout, stretch=3)

        self.vel_plot = pg.PlotWidget()
        self.vel_plot.setTitle("Velocity Profile")
        self.vel_plot.setLabel("bottom", "s (m)")
        self.vel_plot.setLabel("left", "vₓ (m/s)")
        self.vel_plot.plot(s_orig, v_orig, pen=pg.mkPen("lightgray", width=2))
        self.vel_spline = pg.PlotDataItem(pen=pg.mkPen("r", width=4))
        self.vel_plot.addItem(self.vel_spline)
        self.vel_plot.addItem(
            pg.InfiniteLine(
                pos=0, angle=0, pen=pg.mkPen("g", style=Qt.PenStyle.DashLine)
            )
        )
        main_layout.addWidget(self.vel_plot, stretch=2)

        static_scatter = pg.ScatterPlotItem(
            pos=self.static_pts,
            pen=pg.mkPen("lightgray"),
            brush=pg.mkBrush("lightgray"),
            size=8,
        )
        self.plot.addItem(static_scatter)

        xs, ys, vs, S = spline_sample_closed(
            self.static_pts, self.num_samples, self.cs_vel_initial, self.s_max
        )
        self.ctrl_pts = list(zip(xs[:-1], ys[:-1]))
        self.draggable = DraggableScatter(self.ctrl_pts, self.update_spline)
        self.plot.addItem(self.draggable)

        s_ctrl_path = self._get_arc_lengths_for_path_ctrl_pts(self.ctrl_pts)
        v_ctrl_path_initial = self.cs_vel_initial(s_ctrl_path % self.s_max)
        if len(s_ctrl_path) > 1:
            v_ctrl_path_initial[-1] = v_ctrl_path_initial[0]
            self.cs_vel_current = CubicSpline(
                s_ctrl_path, v_ctrl_path_initial, bc_type="periodic"
            )
        elif len(s_ctrl_path) == 1:
            self.cs_vel_current = lambda x: np.full_like(x, v_ctrl_path_initial[0])
        else:
            self.cs_vel_current = lambda x: np.zeros_like(x)

        self.draggable_vel = DraggableVelocityScatter(
            s_ctrl_path,
            v_ctrl_path_initial,
            self.handle_velocity_drag,
            is_periodic=True,
        )
        self.vel_plot.addItem(self.draggable_vel)

        self.spline_curve = pg.PlotDataItem(pen=pg.mkPen("r", width=4))
        self.plot.addItem(self.spline_curve)

        # Initialize colored segments lists for both plots
        self.colored_vel_segments = []
        self.colored_pos_segments = []

        self.update_spline(self.ctrl_pts)

        self.setCentralWidget(container)

    def _get_arc_lengths_for_path_ctrl_pts(self, path_ctrl_pts):
        """
        Calculate arc lengths (s-coordinates) for control points along a path.

        This method computes the cumulative distance along the path for each control point,
        which is used to map between spatial position and path parameter.

        Parameters:
        -----------
        path_ctrl_pts : list of tuples
            List of (x, y) coordinates for the control points

        Returns:
        --------
        numpy.ndarray
            Array of s-coordinates (distances) for each control point
        """
        if not path_ctrl_pts:
            return np.array([])
        pts = np.array(path_ctrl_pts)
        if len(pts) == 1:
            return np.array([0.0])
        closed_pts = np.vstack([pts, pts[0]])
        dx = np.diff(closed_pts[:, 0])
        dy = np.diff(closed_pts[:, 1])
        dist = np.hypot(dx, dy)
        s_coords = np.concatenate(([0], np.cumsum(dist[:-1])))
        return s_coords

    def handle_velocity_drag(self, s_coords, v_coords):
        """
        Update the velocity profile when velocity control points are dragged.

        This method is called when the user drags velocity control points in the velocity plot.
        It updates the cubic spline representing the velocity profile and refreshes both
        the position (x,y) plot and velocity plot with the new colored visualization.

        Parameters:
        -----------
        s_coords : array-like
            Array of s-coordinates (distance along path) for each velocity control point
        v_coords : array-like
            Array of velocity values corresponding to each s-coordinate
        """
        if len(s_coords) > 1:
            v_coords_periodic = np.array(v_coords)
            v_coords_periodic[-1] = v_coords_periodic[0]
            self.cs_vel_current = CubicSpline(
                s_coords, v_coords_periodic, bc_type="periodic"
            )
        elif len(s_coords) == 1:
            self.cs_vel_current = lambda x: np.full_like(x, v_coords[0])
        else:
            self.cs_vel_current = lambda x: np.zeros_like(x)

        xs_spline, ys_spline, vs_spline, S_spline = spline_sample_closed(
            self.ctrl_pts,
            max(200, len(self.ctrl_pts) * 10),
            self.cs_vel_current,
            self.s_max,
        )

        # Remove the existing red spline
        self.plot.removeItem(self.spline_curve)

        # Remove existing colored position segments
        if hasattr(self, "colored_pos_segments"):
            for segment in self.colored_pos_segments:
                self.plot.removeItem(segment)

        # Create new colored segments for position plot based on velocity
        self.colored_pos_segments = []

        if len(xs_spline) > 1 and len(vs_spline) > 1:
            # Create a PyQtGraph ColorMap (green-yellow-red)
            pos = np.array([0.0, 0.5, 1.0])
            colors = np.array(
                [
                    [0, 200, 0, 255],  # Green (low speed)
                    [255, 255, 0, 255],  # Yellow (medium speed)
                    [255, 0, 0, 255],  # Red (high speed)
                ]
            )
            cmap = pg.ColorMap(pos, colors)

            # Add MultiColorLine for position plot
            multi_color_line = MultiColorLine(
                xs_spline, ys_spline, vs_spline, cmap, width=6
            )
            self.plot.addItem(multi_color_line)
            self.colored_pos_segments.append(multi_color_line)
        else:
            # If no data, add back the simple red spline
            self.plot.addItem(self.spline_curve)
            self.spline_curve.setData(xs_spline, ys_spline)

        # Create colored velocity plot
        if len(S_spline) > 1 and len(vs_spline) > 1:
            # Remove existing velocity spline
            if hasattr(self, "vel_spline"):
                self.vel_plot.removeItem(self.vel_spline)

            # Remove existing colored segments
            if hasattr(self, "colored_vel_segments"):
                for segment in self.colored_vel_segments:
                    self.vel_plot.removeItem(segment)

            # Create new colored segments based on velocity
            self.colored_vel_segments = []

            # Create a PyQtGraph ColorMap (green-yellow-red)
            pos = np.array([0.0, 0.5, 1.0])
            colors = np.array(
                [
                    [0, 200, 0, 255],  # Green (low speed)
                    [255, 255, 0, 255],  # Yellow (medium speed)
                    [255, 0, 0, 255],  # Red (high speed)
                ]
            )
            cmap = pg.ColorMap(pos, colors)

            # Add MultiColorLine for velocity plot
            multi_color_line = MultiColorLine(
                S_spline, vs_spline, vs_spline, cmap, width=4
            )
            self.vel_plot.addItem(multi_color_line)
            self.colored_vel_segments.append(multi_color_line)
        else:
            # If no data, just update the red line
            self.vel_spline.setData(S_spline, vs_spline)

    def on_slider_change(self, value):
        """
        Update the number of control points when the slider value changes.

        This method resamples the original static path with a new number of points
        based on the slider value, creating a new set of control points and
        updating the spline visualization.

        Parameters:
        -----------
        value : int
            New number of control points from the slider
        """
        self.num_samples = value
        xs, ys, vs, S = spline_sample_closed(
            self.static_pts, self.num_samples, self.cs_vel_initial, self.s_max
        )
        self.ctrl_pts = list(zip(xs[:-1], ys[:-1]))
        self.draggable.positions = self.ctrl_pts.copy()
        self.draggable.setData(pos=self.ctrl_pts, data=list(range(len(self.ctrl_pts))))
        self.update_spline(self.ctrl_pts)

    def update_spline(self, ctrl_pts):
        """
        Update the spline representation when control points are modified.

        This method is called whenever control points are moved, added, or removed.
        It updates both the position spline and the velocity profile, maintaining
        consistency between them. It also handles the colored visualization of the
        splines based on velocity values.

        Parameters:
        -----------
        ctrl_pts : list of tuples
            List of (x, y) coordinates for the updated control points
        """
        self.ctrl_pts = ctrl_pts
        s_ctrl_path_current = self._get_arc_lengths_for_path_ctrl_pts(self.ctrl_pts)
        if len(s_ctrl_path_current) > 0:
            pts_arr = np.array(self.ctrl_pts)
            if len(pts_arr) > 1:
                closed_path_pts = np.vstack([pts_arr, pts_arr[0]])
                dx = np.diff(closed_path_pts[:, 0])
                dy = np.diff(closed_path_pts[:, 1])
                self.s_max = np.sum(np.hypot(dx, dy))
            elif len(pts_arr) == 1:
                self.s_max = 0.0
            else:
                self.s_max = 0.01
        else:
            self.s_max = 0.01

        if len(self.ctrl_pts) > 0:
            new_s_vel_ctrl = s_ctrl_path_current
            current_s_for_cs_vel = self.draggable_vel.s_coords
            current_v_for_cs_vel = self.draggable_vel.v_coords

            if len(current_s_for_cs_vel) > 1 and current_s_for_cs_vel[-1] > 0:
                unique_s, unique_idx = np.unique(
                    current_s_for_cs_vel, return_index=True
                )
                unique_v = current_v_for_cs_vel[unique_idx]

                if len(unique_s) > 1:
                    s_interp_domain = np.concatenate(
                        (
                            [
                                unique_s[0] - (current_s_for_cs_vel[-1] - unique_s[-2])
                                if len(unique_s) > 2
                                else unique_s[0] - 1
                            ],
                            unique_s,
                            [
                                unique_s[-1] + (unique_s[1] - unique_s[0])
                                if len(unique_s) > 1
                                else unique_s[-1] + 1
                            ],
                        )
                    )
                    v_interp_values = np.concatenate(
                        ([unique_v[0]], unique_v, [unique_v[0]])
                    )
                    interp_func = interp1d(
                        s_interp_domain,
                        v_interp_values,
                        kind="linear",
                        fill_value="extrapolate",
                    )

                    if self.draggable_vel.s_coords[-1] > 1e-6:
                        norm_s_new = new_s_vel_ctrl / (
                            self.s_max if self.s_max > 1e-6 else 1.0
                        )
                        s_to_sample_at = norm_s_new * (
                            self.draggable_vel.s_coords[-1]
                            if len(self.draggable_vel.s_coords) > 0
                            else 1.0
                        )
                        new_v_vel_ctrl = interp_func(s_to_sample_at)
                    else:
                        new_v_vel_ctrl = np.full_like(
                            new_s_vel_ctrl,
                            current_v_for_cs_vel[0]
                            if len(current_v_for_cs_vel) > 0
                            else self.cs_vel_initial(0),
                        )

                elif len(unique_s) == 1:
                    new_v_vel_ctrl = np.full_like(new_s_vel_ctrl, unique_v[0])
                else:
                    new_v_vel_ctrl = self.cs_vel_initial(
                        new_s_vel_ctrl % (self.s_max if self.s_max > 1e-6 else 1.0)
                    )

            elif len(current_s_for_cs_vel) == 1:
                new_v_vel_ctrl = np.full_like(new_s_vel_ctrl, current_v_for_cs_vel[0])
            else:
                new_v_vel_ctrl = self.cs_vel_initial(
                    new_s_vel_ctrl % (self.s_max if self.s_max > 1e-6 else 1.0)
                )

            # Ensure new_v_vel_ctrl is non-negative
            new_v_vel_ctrl = np.maximum(0, new_v_vel_ctrl)

            if len(new_s_vel_ctrl) > 1:
                new_v_vel_ctrl[-1] = new_v_vel_ctrl[0]
                self.cs_vel_current = CubicSpline(
                    new_s_vel_ctrl, new_v_vel_ctrl, bc_type="periodic"
                )
            elif len(new_s_vel_ctrl) == 1:
                self.cs_vel_current = lambda x: np.full_like(x, new_v_vel_ctrl[0])
            else:
                self.cs_vel_current = lambda x: np.zeros_like(x)

            self.draggable_vel.update_positions(new_s_vel_ctrl, new_v_vel_ctrl)
        else:
            self.cs_vel_current = lambda x: np.zeros_like(x)
            self.draggable_vel.update_positions([], [])

        xs, ys, vs, S = spline_sample_closed(
            self.ctrl_pts,
            max(200, len(self.ctrl_pts) * 10),
            self.cs_vel_current,
            self.s_max,
        )

        # Remove the existing simple red spline
        self.plot.removeItem(self.spline_curve)

        # Remove existing colored position segments
        if hasattr(self, "colored_pos_segments"):
            for segment in self.colored_pos_segments:
                self.plot.removeItem(segment)

        # Create new colored segments for position plot based on velocity
        self.colored_pos_segments = []

        if len(xs) > 1 and len(vs) > 1:
            # Create a PyQtGraph ColorMap (green-yellow-red)
            pos = np.array([0.0, 0.5, 1.0])
            colors = np.array(
                [
                    [0, 200, 0, 255],  # Green (low speed)
                    [255, 255, 0, 255],  # Yellow (medium speed)
                    [255, 0, 0, 255],  # Red (high speed)
                ]
            )
            cmap = pg.ColorMap(pos, colors)

            # Add MultiColorLine for position plot
            multi_color_line = MultiColorLine(xs, ys, vs, cmap, width=6)
            self.plot.addItem(multi_color_line)
            self.colored_pos_segments.append(multi_color_line)
        else:
            # If no data, add back the simple red spline
            self.plot.addItem(self.spline_curve)
            self.spline_curve.setData(xs, ys)

        # Create colored velocity plot
        if len(S) > 1 and len(vs) > 1:
            # Remove existing velocity spline
            if hasattr(self, "vel_spline"):
                self.vel_plot.removeItem(self.vel_spline)

            # Remove existing colored segments
            if hasattr(self, "colored_vel_segments"):
                for segment in self.colored_vel_segments:
                    self.vel_plot.removeItem(segment)

            # Create new colored segments based on velocity
            self.colored_vel_segments = []

            # Create a PyQtGraph ColorMap (similar to viridis colormap)
            pos = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
            colors = np.array(
                [
                    [0, 0, 100, 255],  # Dark blue
                    [0, 100, 100, 255],  # Teal blue
                    [0, 200, 0, 255],  # Green
                    [200, 100, 0, 255],  # Orange
                    [255, 255, 0, 255],  # Yellow
                ]
            )
            cmap = pg.ColorMap(pos, colors)

            # Add MultiColorLine for velocity plot
            multi_color_line = MultiColorLine(S, vs, vs, cmap, width=4)
            self.vel_plot.addItem(multi_color_line)
            self.colored_vel_segments.append(multi_color_line)
        else:
            # If no data, just update the red line
            self.vel_spline.setData(S, vs)

        count = len(self.ctrl_pts)
        self.slider.blockSignals(True)
        self.slider.setValue(min(count, self.slider.maximum()))
        self.slider.blockSignals(False)
        self.slider_label.setText(f"Points: {count}")

    def save_csv(self):
        """
        Save the modified trajectory to a CSV file.

        This method samples the current spline at a high resolution (1000 points)
        and computes all necessary trajectory parameters including:
        - Position (x, y)
        - Path length (s)
        - Heading angle (psi)
        - Curvature (kappa)
        - Velocity (vx)
        - Acceleration (ax)

        The trajectory is saved to a new file with "_modified" appended to the original filename.
        """
        new_csv_path = self.csv_path.replace(".csv", "_modified.csv")

        xs, ys, vs, S = spline_sample_closed(
            self.ctrl_pts, 1000, self.cs_vel_current, self.s_max
        )
        x_np = xs
        y_np = ys
        vx_np = vs
        dx = np.gradient(x_np, edge_order=2)
        dy = np.gradient(y_np, edge_order=2)
        psi = np.arctan2(dy, dx)
        x_ext = np.concatenate((x_np[-2:], x_np, x_np[:2]))
        y_ext = np.concatenate((y_np[-2:], y_np, y_np[:2]))
        dx_dt = np.gradient(x_ext, edge_order=2)
        dy_dt = np.gradient(y_ext, edge_order=2)
        d2x = np.gradient(dx_dt, edge_order=2)
        d2y = np.gradient(dy_dt, edge_order=2)
        denom = (dx_dt * dx_dt + dy_dt * dy_dt) ** 1.5
        denom[denom == 0] = np.finfo(float).eps
        kappa_full = (dx_dt * d2y - d2x * dy_dt) / denom
        kappa = kappa_full[2:-2]
        ax = np.gradient(vx_np, edge_order=2)
        df_out = pd.DataFrame(
            {
                "s_m": S,
                "x_m": x_np,
                "y_m": y_np,
                "psi_rad": psi,
                "kappa_radpm": kappa,
                "vx_mps": vx_np,
                "ax_mps2": ax,
            }
        )
        with open(new_csv_path, "w") as f:
            f.write("#\n")
            f.write("#\n")
            f.write("# s_m; x_m; y_m; psi_rad; kappa_radpm; vx_mps; ax_mps2\n")
        df_out.to_csv(
            new_csv_path,
            sep=";",
            index=False,
            header=False,
            mode="a",
            float_format="%.6f",
        )
        print(f"Saved {len(df_out)} interpolated points to {new_csv_path}")

    def reset_velocity_profile(self):
        """
        Reset velocity control points to velocities of closest original XY waypoints.

        This method restores the velocity profile to match the original waypoints' velocities.
        For each control point in the current path, it:
        1. Finds the closest waypoint in the original (static) path
        2. Assigns the velocity from that original waypoint
        3. Updates the velocity spline and visualizations

        This is useful for reverting velocity changes while maintaining the current path shape.
        """
        s_ctrl = self._get_arc_lengths_for_path_ctrl_pts(self.ctrl_pts)
        # Find closest static waypoint for each control point
        static_arr = np.array(self.static_pts)
        v_new = []
        for x, y in self.ctrl_pts:
            dists = np.hypot(static_arr[:, 0] - x, static_arr[:, 1] - y)
            idx = int(np.argmin(dists))
            v_new.append(self.v_orig[idx])
        v_new = np.maximum(0, np.array(v_new))  # clamp non-negative
        # Enforce periodicity
        if len(s_ctrl) > 1:
            v_new[-1] = v_new[0]
            self.cs_vel_current = CubicSpline(s_ctrl, v_new, bc_type="periodic")
        elif len(s_ctrl) == 1:
            self.cs_vel_current = lambda x: np.full_like(x, v_new[0])
        else:
            self.cs_vel_current = lambda x: np.zeros_like(x)

        # Update draggable velocity scatter
        self.draggable_vel.update_positions(s_ctrl, v_new)

        # Sample the updated spline
        xs, ys, vs, S = spline_sample_closed(
            self.ctrl_pts,
            max(200, len(self.ctrl_pts) * 10),
            self.cs_vel_current,
            self.s_max,
        )

        # Update position plot with colored spline
        # Remove the existing simple red spline
        self.plot.removeItem(self.spline_curve)

        # Remove existing colored position segments
        if hasattr(self, "colored_pos_segments"):
            for segment in self.colored_pos_segments:
                self.plot.removeItem(segment)

        # Create new colored segments for position plot based on velocity
        self.colored_pos_segments = []

        if len(xs) > 1 and len(vs) > 1:
            # Create a PyQtGraph ColorMap (green-yellow-red)
            pos = np.array([0.0, 0.5, 1.0])
            colors = np.array(
                [
                    [0, 200, 0, 255],  # Green (low speed)
                    [255, 255, 0, 255],  # Yellow (medium speed)
                    [255, 0, 0, 255],  # Red (high speed)
                ]
            )
            cmap = pg.ColorMap(pos, colors)

            # Add MultiColorLine for position plot
            multi_color_line = MultiColorLine(xs, ys, vs, cmap, width=6)
            self.plot.addItem(multi_color_line)
            self.colored_pos_segments.append(multi_color_line)
        else:
            # If no data, add back the simple red spline
            self.plot.addItem(self.spline_curve)
            self.spline_curve.setData(xs, ys)

        # Update velocity plot with colored spline
        # Remove existing velocity spline
        if hasattr(self, "vel_spline"):
            self.vel_plot.removeItem(self.vel_spline)

        # Remove existing colored segments
        if hasattr(self, "colored_vel_segments"):
            for segment in self.colored_vel_segments:
                self.vel_plot.removeItem(segment)

        # Create new colored segments based on velocity
        self.colored_vel_segments = []

        if len(S) > 1 and len(vs) > 1:
            # Create a PyQtGraph ColorMap (green-yellow-red)
            pos = np.array([0.0, 0.5, 1.0])
            colors = np.array(
                [
                    [0, 200, 0, 255],  # Green (low speed)
                    [255, 255, 0, 255],  # Yellow (medium speed)
                    [255, 0, 0, 255],  # Red (high speed)
                ]
            )
            cmap = pg.ColorMap(pos, colors)

            # Add MultiColorLine for velocity plot
            multi_color_line = MultiColorLine(S, vs, vs, cmap, width=4)
            self.vel_plot.addItem(multi_color_line)
            self.colored_vel_segments.append(multi_color_line)
        else:
            # If no data, just update the red line
            self.vel_spline.setData(S, vs)


if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Get CSV path from command line argument if provided
    csv_path = "../traj_examples/new_loc.csv"  # Default path
    if len(sys.argv) > 1:
        csv_path = sys.argv[1]

    w = MainWindow(csv_path)
    w.resize(1000, 600)
    w.show()
    sys.exit(app.exec())
