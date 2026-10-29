import csv
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tkinter as tk
from tkinter import ttk
from PIL import Image

from app import (
    FlowVelocityApp,
    adjust_image_for_display,
    calculate_velocity,
    projected_displacement,
    run_self_test,
)


class CalculationTests(unittest.TestCase):
    def test_release_self_test(self):
        self.assertEqual(run_self_test(), 0)

    def test_horizontal_projection_ignores_vertical_motion(self):
        displacement, pixels = projected_displacement(
            before=(10, 10),
            after=(30, 999),
            calibration_start=(0, 0),
            calibration_end=(100, 0),
            real_length=10,
        )
        self.assertAlmostEqual(pixels, 100)
        self.assertAlmostEqual(displacement, 2)

    def test_diagonal_calibration_projection(self):
        displacement, _ = projected_displacement(
            before=(0, 0),
            after=(10, 10),
            calibration_start=(0, 0),
            calibration_end=(100, 100),
            real_length=20,
        )
        self.assertAlmostEqual(displacement, 2)

    def test_velocity_uses_fps_and_frame_gap(self):
        displacement, velocity, _ = calculate_velocity(
            before=(0, 0),
            after=(20, 50),
            calibration_start=(0, 0),
            calibration_end=(100, 0),
            real_length=10,
            fps=240,
            frame_gap=2,
        )
        self.assertAlmostEqual(displacement, 2)
        self.assertAlmostEqual(velocity, 240)

    def test_negative_direction(self):
        displacement, velocity, _ = calculate_velocity(
            before=(50, 0),
            after=(40, 0),
            calibration_start=(0, 0),
            calibration_end=(100, 0),
            real_length=10,
            fps=100,
            frame_gap=1,
        )
        self.assertAlmostEqual(displacement, -1)
        self.assertAlmostEqual(velocity, -100)

    def test_display_adjustments_do_not_modify_source(self):
        source = Image.new("RGB", (2, 1))
        source.putdata([(50, 50, 50), (200, 200, 200)])
        adjusted = adjust_image_for_display(source, brightness=1.5, contrast=1.4)

        source_pixels = [source.getpixel((0, 0)), source.getpixel((1, 0))]
        adjusted_pixels = [adjusted.getpixel((0, 0)), adjusted.getpixel((1, 0))]
        self.assertEqual(source_pixels, [(50, 50, 50), (200, 200, 200)])
        self.assertNotEqual(adjusted_pixels, source_pixels)
        self.assertLess(adjusted.getpixel((0, 0))[0], adjusted.getpixel((1, 0))[0])


class GuiBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = FlowVelocityApp(self.root)
        self.app.images = [Path("frame001.jpg"), Path("frame002.jpg")]
        self.app.calibration_points = [(0, 0), (100, 0)]
        self.app.real_length_var.set(10)
        self.app.fps_var.set(240)
        self.root.update()

    def tearDown(self):
        self.app._cancel_pending_jobs()
        self.root.destroy()

    def _set_particle(self):
        self.app.track_before = (0, 0)
        self.app.track_after = (10, 50)

    def test_saved_results_follow_calibration_change(self):
        self._set_particle()
        self.assertTrue(self.app.record_measurement())
        self.assertAlmostEqual(self.app.measurements[0].velocity, 240)

        self.app.real_length_var.set(20)
        self.root.update()

        self.assertAlmostEqual(self.app.measurements[0].displacement, 2)
        self.assertAlmostEqual(self.app.measurements[0].velocity, 480)

    def test_multiple_measurements_selection_and_clear(self):
        self._set_particle()
        self.app.record_same_pair()
        self._set_particle()
        self.app.record_same_pair()
        self.assertEqual(len(self.app.measurements), 2)
        self.assertIsNone(self.app.track_before)
        self.assertIsNone(self.app.track_after)

        self.app.select_all_results()
        self.assertEqual(len(self.app.table.selection()), 2)

        with patch("app.messagebox.askyesno", return_value=True):
            self.app.clear_all_results()
        self.assertEqual(self.app.measurements, [])
        self.assertEqual(self.app.table.get_children(), ())

    def test_skip_advances_without_recording(self):
        self.app.images.append(Path("frame003.jpg"))
        self._set_particle()

        with patch.object(self.app, "_refresh_pair"):
            self.app.skip_to_next_pair()

        self.assertEqual(self.app.current_index, 1)
        self.assertEqual(self.app.measurements, [])
        self.assertIsNone(self.app.track_before)
        self.assertIsNone(self.app.track_after)

    def test_result_row_navigation_restores_pair_and_points(self):
        self._set_particle()
        self.app.record_measurement()
        item = self.app.table.get_children()[0]
        self.app.table.selection_set(item)
        self.app.table.focus(item)
        self.app.current_index = 1
        self.app.track_before = None
        self.app.track_after = None

        with patch.object(self.app, "_refresh_pair"):
            self.app.go_to_selected_result()

        self.assertEqual(self.app.current_index, 0)
        self.assertEqual(self.app.track_before, (0, 0))
        self.assertEqual(self.app.track_after, (10, 50))

    def test_csv_import_restores_exported_measurement(self):
        self._set_particle()
        self.app.record_measurement()
        original = self.app.measurements[0]
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "results.csv"
            fields = list(asdict(original).keys())
            with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                writer.writerow(asdict(original))

            imported, skipped = self.app._read_measurements_csv(csv_path)

        self.assertEqual(skipped, 0)
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].before_file, "frame001.jpg")
        self.assertEqual(imported[0].after_file, "frame002.jpg")
        self.assertAlmostEqual(imported[0].velocity, original.velocity)
        self.assertEqual(imported[0].after_x_px, 10)

    def test_previous_pair_shows_saved_particle_markers(self):
        self.app.images.append(Path("frame003.jpg"))
        self._set_particle()
        self.app.record_measurement()
        self.app.current_index = 1

        with patch.object(self.app, "_refresh_pair"):
            self.app.previous_pair()

        saved_before = [
            overlay for overlay in self.app.get_overlays("before")
            if overlay.get("label") == "記録No.1"
        ]
        saved_after = [
            overlay for overlay in self.app.get_overlays("after")
            if overlay.get("label") == "記録No.1"
        ]
        self.assertEqual(saved_before[0]["p"], (0, 0))
        self.assertEqual(saved_after[0]["p"], (10, 50))

    def test_image_click_moves_keyboard_focus_from_navigation_button(self):
        event = SimpleNamespace(x=10, y=10)
        with patch.object(
            self.app.before_canvas.canvas, "focus_set"
        ) as focus_set:
            self.app.before_canvas._left_click(event)
        focus_set.assert_called_once_with()

    def test_enter_records_after_skip(self):
        self.app.images.extend([
            Path("frame003.jpg"),
            Path("frame004.jpg"),
        ])
        with patch.object(self.app, "_refresh_pair"):
            self.app.skip_to_next_pair()
        self._set_particle()

        with patch.object(self.app, "_refresh_pair"):
            result = self.app._handle_enter(SimpleNamespace())

        self.assertEqual(result, "break")
        self.assertEqual(len(self.app.measurements), 1)
        self.assertEqual(self.app.measurements[0].before_frame, 2)
        self.assertEqual(self.app.measurements[0].after_frame, 3)

    def test_clicks_survive_display_state_refresh_before_recording(self):
        self.app.mode = "measurement"
        self.app.handle_image_click("before", (0, 0))
        self.app.handle_image_click("after", (10, 50))
        with patch.object(self.app, "_refresh_pair"):
            self.app._gap_changed()
        self.assertIsNone(self.app.track_before)
        self.assertIsNone(self.app.track_after)

        self.assertTrue(self.app.record_measurement())
        self.assertEqual(len(self.app.measurements), 1)
        self.assertEqual(self.app.measurements[0].before_x_px, 0)
        self.assertEqual(self.app.measurements[0].after_x_px, 10)

    def test_before_reference_marker_on_after_can_be_hidden(self):
        self.app.mode = "measurement"
        self.app.handle_image_click("before", (25, 30))

        visible_markers = [
            overlay for overlay in self.app.get_overlays("after")
            if overlay.get("label") == "before位置（参考）"
        ]
        self.assertEqual(len(visible_markers), 1)
        self.assertEqual(visible_markers[0]["p"], (25, 30))
        self.assertEqual(visible_markers[0]["type"], "cross")

        self.app.show_before_position_var.set(False)
        hidden_markers = [
            overlay for overlay in self.app.get_overlays("after")
            if overlay.get("label") == "before位置（参考）"
        ]
        self.assertEqual(hidden_markers, [])

    def test_arrow_keys_repeat_image_navigation(self):
        self.app.images.extend([
            Path("frame003.jpg"),
            Path("frame004.jpg"),
            Path("frame005.jpg"),
        ])
        event = SimpleNamespace(widget=self.app.before_canvas.canvas)
        with patch.object(self.app, "_refresh_pair"):
            self.assertEqual(self.app._handle_right_arrow(event), "break")
            self.assertEqual(self.app._handle_right_arrow(event), "break")
            self.assertEqual(self.app.current_index, 2)
            self.assertEqual(self.app._handle_left_arrow(event), "break")
        self.assertEqual(self.app.current_index, 1)
        self.assertEqual(self.app.measurements, [])

    def test_arrow_keys_do_not_interfere_with_value_inputs(self):
        entry = ttk.Entry(self.root)
        event = SimpleNamespace(widget=entry)
        with patch.object(self.app, "skip_to_next_pair") as skip:
            self.assertIsNone(self.app._handle_right_arrow(event))
        skip.assert_not_called()

    def test_seekbar_moves_directly_and_clears_transient_points(self):
        self.app.images.extend([
            Path("frame003.jpg"),
            Path("frame004.jpg"),
            Path("frame005.jpg"),
            Path("frame006.jpg"),
        ])
        self._set_particle()

        with patch.object(self.app, "_refresh_pair") as refresh:
            self.app._apply_seekbar_position(4)

        self.assertEqual(self.app.current_index, 4)
        self.assertIsNone(self.app.track_before)
        self.assertIsNone(self.app.track_after)
        refresh.assert_called_once_with()

    def test_seekbar_drag_does_not_load_until_mouse_release(self):
        self.app.images.extend([
            Path("frame003.jpg"),
            Path("frame004.jpg"),
            Path("frame005.jpg"),
            Path("frame006.jpg"),
        ])
        with patch.object(self.app, "_apply_seekbar_position") as apply_position:
            self.app._seekbar_changed("1")
            self.app._seekbar_changed("4")
            self.root.update()
            apply_position.assert_not_called()

            self.app.seek_var.set(4)
            self.app._seekbar_released()

        apply_position.assert_called_once_with(4)

    def test_seekbar_range_and_label_follow_frame_gap(self):
        self.app.images.extend([
            Path("frame003.jpg"),
            Path("frame004.jpg"),
            Path("frame005.jpg"),
        ])
        self.app.frame_gap_var.set(2)
        self.app.current_index = 2
        self.app._sync_seekbar_from_index()

        self.assertEqual(float(self.app.seek_scale.cget("to")), 2.0)
        self.assertEqual(self.app.seek_var.get(), 2.0)
        self.assertEqual(
            self.app.seek_label_var.get(), "before 3/5　after 5/5"
        )
        self.assertEqual(self.app.frame_number_var.get(), 3)
        self.assertEqual(float(self.app.frame_number_spinbox.cget("to")), 3.0)

    def test_manual_frame_number_moves_to_requested_before_image(self):
        self.app.images.extend([
            Path("frame003.jpg"),
            Path("frame004.jpg"),
            Path("frame005.jpg"),
        ])
        self.app.frame_number_var.set(4)

        with patch.object(self.app, "_refresh_pair") as refresh:
            self.app.go_to_frame_number()

        self.assertEqual(self.app.current_index, 3)
        refresh.assert_called_once_with()

    def test_manual_frame_number_rejects_unavailable_pair(self):
        self.app.images.append(Path("frame003.jpg"))
        self.app.frame_number_var.set(3)

        with patch("app.messagebox.showinfo") as showinfo:
            self.app.go_to_frame_number()

        self.assertEqual(self.app.current_index, 0)
        showinfo.assert_called_once()

    def test_next_pair_preserves_zoom_and_pan(self):
        with tempfile.TemporaryDirectory() as temporary_folder:
            folder = Path(temporary_folder)
            self.app.images = []
            for index in range(3):
                image_path = folder / f"frame{index + 1:03d}.jpg"
                Image.new("RGB", (100, 60), (60 + index * 20, 80, 100)).save(
                    image_path
                )
                self.app.images.append(image_path)
            self.app._refresh_pair()
            self.root.update()

            self.app.before_canvas.scale = 2.4
            self.app.before_canvas.offset_x = -135.0
            self.app.before_canvas.offset_y = -42.0
            self.app.after_canvas.scale = 3.1
            self.app.after_canvas.offset_x = -210.0
            self.app.after_canvas.offset_y = -75.0
            self.app.skip_to_next_pair()
            self.root.update()

        self.assertAlmostEqual(self.app.before_canvas.scale, 2.4)
        self.assertAlmostEqual(self.app.before_canvas.offset_x, -135.0)
        self.assertAlmostEqual(self.app.before_canvas.offset_y, -42.0)
        self.assertAlmostEqual(self.app.after_canvas.scale, 3.1)
        self.assertAlmostEqual(self.app.after_canvas.offset_x, -210.0)
        self.assertAlmostEqual(self.app.after_canvas.offset_y, -75.0)

    def test_autosave_restores_measurements_settings_and_position(self):
        with tempfile.TemporaryDirectory() as temporary_folder:
            temporary_path = Path(temporary_folder)
            image_folder = temporary_path / "images"
            autosave_folder = temporary_path / "autosave"
            image_folder.mkdir()
            image_paths = [
                image_folder / "frame001.jpg",
                image_folder / "frame002.jpg",
                image_folder / "frame003.jpg",
            ]
            for index, image_path in enumerate(image_paths):
                Image.new("RGB", (20, 10), (50 + index * 20, 80, 100)).save(
                    image_path
                )

            self.app.autosave_dir = autosave_folder
            self.app.folder = image_folder
            self.app.images = image_paths
            self.app.calibration_points = [(1, 2), (11, 2)]
            self.app.mode = "measurement"
            self.app.brightness_var.set(1.4)
            self.app.contrast_var.set(1.7)
            self.app.track_before = (4, 5)
            self.app.track_after = (7, 8)
            self.assertTrue(self.app.record_measurement())
            self.app.current_index = 1
            self.assertTrue(self.app._save_autosave_now())

            autosave_path = self.app._autosave_path_for_folder(image_folder)
            self.assertTrue(autosave_path.exists())
            data = self.app._load_autosave_data(image_folder)
            self.assertIsNotNone(data)

            self.app.measurements.clear()
            self.app.calibration_points.clear()
            self.app.current_index = 0
            self.app.brightness_var.set(1.0)
            self.app.contrast_var.set(1.0)
            self.assertTrue(self.app._restore_autosave_data(data))

            self.assertEqual(len(self.app.measurements), 1)
            self.assertEqual(self.app.calibration_points, [(1.0, 2.0), (11.0, 2.0)])
            self.assertEqual(self.app.current_index, 1)
            self.assertAlmostEqual(self.app.brightness_var.get(), 1.4)
            self.assertAlmostEqual(self.app.contrast_var.get(), 1.7)
            self.assertEqual(self.app.measurements[0].before_file, "frame001.jpg")


if __name__ == "__main__":
    unittest.main()
