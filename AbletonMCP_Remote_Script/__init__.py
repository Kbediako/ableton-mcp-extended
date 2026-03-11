# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import socket
import json
import threading
import time
import traceback
import math

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

try:
    string_types = (basestring,)
except NameError:
    string_types = (str,)

# Constants for socket communication
DEFAULT_PORT = 9877
HOST = "localhost"
BRIDGE_VERSION = "2026-03-11-v4"

class _NamedParameterOwner(object):
    """Small wrapper for non-device automation owners like mixer controls."""

    def __init__(self, name):
        self.name = name

def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)

class AbletonMCP(ControlSurface):
    """AbletonMCP Remote Script for Ableton Live"""
    
    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message("AbletonMCP Remote Script initializing... version " + BRIDGE_VERSION)
        
        # Socket server for communication
        self.server = None
        self.client_threads = []
        self.server_thread = None
        self.running = False
        
        # Cache the song reference for easier access
        self._song = self.song()
        
        # Start the socket server
        self.start_server()
        
        self.log_message("AbletonMCP initialized version " + BRIDGE_VERSION)
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: " + BRIDGE_VERSION + " listening on port " + str(DEFAULT_PORT))
    
    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False
        
        # Stop the server
        if self.server:
            try:
                self.server.close()
            except:
                pass
        
        # Wait for the server thread to exit
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(1.0)
            
        # Clean up any client threads
        for client_thread in self.client_threads[:]:
            if client_thread.is_alive():
                # We don't join them as they might be stuck
                self.log_message("Client thread still alive during disconnect")
        
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")

    def _make_json_safe(self, value):
        """Best-effort conversion for Live objects that leak into responses."""
        if value is None or isinstance(value, (bool, int, float)):
            return value

        if isinstance(value, string_types):
            return value

        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                result[str(key)] = self._make_json_safe(item)
            return result

        if isinstance(value, (list, tuple)):
            return [self._make_json_safe(item) for item in value]

        if isinstance(value, set):
            return [self._make_json_safe(item) for item in sorted(list(value), key=lambda item: str(item))]

        if hasattr(value, "value") and hasattr(value, "display_value"):
            try:
                return {
                    "value": self._make_json_safe(value.value),
                    "display_value": self._make_json_safe(value.display_value)
                }
            except Exception:
                pass

        if hasattr(value, "name"):
            try:
                return getattr(value, "name")
            except Exception:
                pass

        try:
            return int(value)
        except Exception:
            pass

        try:
            return float(value)
        except Exception:
            pass

        try:
            return repr(value)
        except Exception:
            return "<unserializable>"
    
    def start_server(self):
        """Start the socket server in a separate thread"""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)  # Allow up to 5 pending connections
            
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))
    
    def _server_thread(self):
        """Server thread implementation - handles client connections"""
        try:
            self.log_message("Server thread started")
            # Set a timeout to allow regular checking of running flag
            self.server.settimeout(1.0)
            
            while self.running:
                try:
                    # Accept connections with timeout
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    self.show_message("AbletonMCP: Client connected")
                    
                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                    # Keep track of client threads
                    self.client_threads.append(client_thread)
                    
                    # Clean up finished client threads
                    self.client_threads = [t for t in self.client_threads if t.is_alive()]
                    
                except socket.timeout:
                    # No connection yet, just continue
                    continue
                except Exception as e:
                    if self.running:  # Only log if still running
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)
            
            self.log_message("Server thread stopped")
        except Exception as e:
            self.log_message("Server thread error: " + str(e))
    
    def _handle_client(self, client):
        """Handle communication with a connected client"""
        self.log_message("Client handler started")
        client.settimeout(None)  # No timeout for client socket
        buffer = ''  # Changed from b'' to '' for Python 2
        
        try:
            while self.running:
                try:
                    # Receive data
                    data = client.recv(8192)
                    
                    if not data:
                        # Client disconnected
                        self.log_message("Client disconnected")
                        break
                    
                    # Accumulate data in buffer with explicit encoding/decoding
                    try:
                        # Python 3: data is bytes, decode to string
                        buffer += data.decode('utf-8')
                    except AttributeError:
                        # Python 2: data is already string
                        buffer += data
                    
                    try:
                        # Try to parse command from buffer
                        command = json.loads(buffer)  # Removed decode('utf-8')
                        buffer = ''  # Clear buffer after successful parse
                        
                        self.log_message("Received command: " + str(command.get("type", "unknown")))
                        
                        # Process the command and get response
                        response = self._process_command(command)
                        
                        # Send the response with explicit encoding
                        try:
                            # Python 3: encode string to bytes
                            client.sendall(json.dumps(response).encode('utf-8'))
                        except AttributeError:
                            # Python 2: string is already bytes
                            client.sendall(json.dumps(response))
                    except ValueError:
                        # Incomplete data, wait for more
                        continue
                        
                except Exception as e:
                    self.log_message("Error handling client data: " + str(e))
                    self.log_message(traceback.format_exc())
                    
                    # Send error response if possible
                    error_response = {
                        "status": "error",
                        "message": str(e)
                    }
                    try:
                        # Python 3: encode string to bytes
                        client.sendall(json.dumps(error_response).encode('utf-8'))
                    except AttributeError:
                        # Python 2: string is already bytes
                        client.sendall(json.dumps(error_response))
                    except:
                        # If we can't send the error, the connection is probably dead
                        break
                    
                    # For serious errors, break the loop
                    if not isinstance(e, ValueError):
                        break
        except Exception as e:
            self.log_message("Error in client handler: " + str(e))
        finally:
            try:
                client.close()
            except:
                pass
            self.log_message("Client handler stopped")
    
    def _process_command(self, command):
        """Process a command from the client and return a response"""
        command_type = command.get("type", "")
        params = command.get("params", {})
        
        # Initialize response
        response = {
            "status": "success",
            "result": {}
        }
        
        try:
            # Route the command to the appropriate handler
            if command_type == "get_session_info":
                response["result"] = self._get_session_info()
            elif command_type == "get_song_state":
                response["result"] = self._get_song_state()
            elif command_type == "get_song_overview":
                response["result"] = self._get_song_overview()
            elif command_type == "get_view_state":
                response["result"] = self._get_view_state()
            elif command_type == "get_scenes":
                response["result"] = self._get_scenes()
            elif command_type == "get_scene_info":
                scene_index = params.get("scene_index", 0)
                response["result"] = self._get_scene_info(scene_index)
            elif command_type == "get_cue_points":
                response["result"] = self._get_cue_points()
            elif command_type == "get_visible_tracks":
                response["result"] = self._get_visible_tracks()
            elif command_type == "get_track_info":
                track_index = params.get("track_index", 0)
                track_scope = params.get("track_scope", "track")
                response["result"] = self._get_track_info(track_index, track_scope)
            elif command_type == "get_track_mixer":
                track_index = params.get("track_index", 0)
                track_scope = params.get("track_scope", "track")
                response["result"] = self._get_track_mixer(track_index, track_scope)
            elif command_type == "get_track_view":
                track_index = params.get("track_index", 0)
                track_scope = params.get("track_scope", "track")
                response["result"] = self._get_track_view(track_index, track_scope)
            elif command_type == "get_track_sends":
                track_index = params.get("track_index", 0)
                track_scope = params.get("track_scope", "track")
                response["result"] = self._get_track_sends(track_index, track_scope)
            elif command_type == "get_arrangement_clips":
                track_index = params.get("track_index", 0)
                track_scope = params.get("track_scope", "track")
                response["result"] = self._get_arrangement_clips(track_index, track_scope)
            elif command_type == "get_take_lanes":
                track_index = params.get("track_index", 0)
                track_scope = params.get("track_scope", "track")
                response["result"] = self._get_take_lanes(track_index, track_scope)
            elif command_type == "get_track_routing":
                track_index = params.get("track_index", 0)
                track_scope = params.get("track_scope", "track")
                response["result"] = self._get_track_routing(track_index, track_scope)
            elif command_type == "get_clip_slot_info":
                track_index = params.get("track_index", 0)
                clip_index = params.get("clip_index", 0)
                track_scope = params.get("track_scope", "track")
                response["result"] = self._get_clip_slot_info(track_index, clip_index, track_scope)
            elif command_type == "get_clip_info":
                track_index = params.get("track_index", 0)
                clip_index = params.get("clip_index", 0)
                arrangement = params.get("arrangement", False)
                arrangement_clip_index = params.get("arrangement_clip_index", 0)
                track_scope = params.get("track_scope", "track")
                response["result"] = self._get_clip_info(
                    track_index,
                    clip_index,
                    arrangement,
                    arrangement_clip_index,
                    track_scope
                )
            elif command_type == "get_device_topology":
                track_index = params.get("track_index", 0)
                track_scope = params.get("track_scope", "track")
                container_path = params.get("container_path", [])
                max_depth = params.get("max_depth", 4)
                include_parameters = params.get("include_parameters", False)
                include_empty_drum_pads = params.get("include_empty_drum_pads", False)
                response["result"] = self._get_device_topology(
                    track_index,
                    track_scope,
                    container_path,
                    max_depth,
                    include_parameters,
                    include_empty_drum_pads
                )
            elif command_type == "sample_clip_automation":
                track_index = params.get("track_index", 0)
                clip_index = params.get("clip_index", 0)
                arrangement = params.get("arrangement", False)
                arrangement_clip_index = params.get("arrangement_clip_index", 0)
                device_index = params.get("device_index", 0)
                parameter_name = params.get("parameter_name", "")
                parameter_source = params.get("parameter_source", "device")
                send_index = params.get("send_index", 0)
                sample_times = params.get("sample_times", [])
                track_scope = params.get("track_scope", "track")
                container_path = params.get("container_path", [])
                response["result"] = self._sample_clip_automation(
                    track_index,
                    clip_index,
                    arrangement,
                    arrangement_clip_index,
                    device_index,
                    parameter_name,
                    parameter_source,
                    send_index,
                    sample_times,
                    track_scope,
                    container_path
                )
            elif command_type == "get_clip_automation_events":
                track_index = params.get("track_index", 0)
                clip_index = params.get("clip_index", 0)
                arrangement = params.get("arrangement", False)
                arrangement_clip_index = params.get("arrangement_clip_index", 0)
                device_index = params.get("device_index", 0)
                parameter_name = params.get("parameter_name", "")
                parameter_source = params.get("parameter_source", "device")
                send_index = params.get("send_index", 0)
                start_time = params.get("start_time", 0.0)
                end_time = params.get("end_time", 0.0)
                track_scope = params.get("track_scope", "track")
                container_path = params.get("container_path", [])
                response["result"] = self._get_clip_automation_events(
                    track_index,
                    clip_index,
                    arrangement,
                    arrangement_clip_index,
                    device_index,
                    parameter_name,
                    parameter_source,
                    send_index,
                    start_time,
                    end_time,
                    track_scope,
                    container_path
                )
            elif command_type == "debug_object_methods":
                object_type = params.get("object_type", "")
                track_index = params.get("track_index", 0)
                clip_index = params.get("clip_index", 0)
                device_index = params.get("device_index", 0)
                parameter_name = params.get("parameter_name", "")
                parameter_source = params.get("parameter_source", "device")
                send_index = params.get("send_index", 0)
                arrangement_clip_index = params.get("arrangement_clip_index", 0)
                arrangement = params.get("arrangement", False)
                track_scope = params.get("track_scope", "track")
                container_path = params.get("container_path", [])
                response["result"] = self._debug_object_methods(
                    object_type,
                    track_index,
                    arrangement_clip_index,
                    clip_index,
                    device_index,
                    parameter_name,
                    arrangement,
                    track_scope,
                    container_path,
                    parameter_source,
                    send_index
                )
            elif command_type == "get_clip_notes":
                track_index = params.get("track_index", 0)
                clip_index = params.get("clip_index", 0)
                arrangement = params.get("arrangement", False)
                arrangement_clip_index = params.get("arrangement_clip_index", 0)
                response["result"] = self._get_clip_notes(
                    track_index,
                    clip_index,
                    arrangement,
                    arrangement_clip_index
                )
            elif command_type == "get_device_parameters":
                track_index = params.get("track_index", 0)
                device_index = params.get("device_index", 0)
                track_scope = params.get("track_scope", "track")
                container_path = params.get("container_path", [])
                response["result"] = self._get_device_parameters(track_index, device_index, track_scope, container_path)
            elif command_type == "get_device_input_routing":
                track_index = params.get("track_index", 0)
                device_index = params.get("device_index", 0)
                track_scope = params.get("track_scope", "track")
                container_path = params.get("container_path", [])
                response["result"] = self._get_device_input_routing(track_index, device_index, track_scope, container_path)
            elif command_type == "get_level_snapshot":
                response["result"] = self._get_level_snapshot()
            elif command_type == "get_supported_commands":
                response["result"] = self._get_supported_commands()
            # Commands that modify Live's state should be scheduled on the main thread
            elif command_type in ["create_midi_track", "create_audio_track", "create_return_track", "delete_return_track",
                                 "create_scene", "duplicate_scene", "delete_scene",
                                 "duplicate_track", "set_track_name", "set_track_color", "delete_track",
                                 "delete_device", "duplicate_device", "set_device_parameters",
                                 "insert_device", "move_device", "duplicate_clip_to_arrangement",
                                 "clear_clip_automation", "set_clip_automation_steps", "delete_clip_automation_events",
                                 "create_clip", "create_take_lane", "create_arrangement_audio_clip", "create_arrangement_midi_clip",
                                 "add_notes_to_clip", "set_clip_name",
                                 "set_clip_color", "sync_track_media_colors", "sync_all_media_colors",
                                 "split_arrangement_audio_track_by_clip_name", "set_device_parameter",
                                 "delete_clip_in_slot", "duplicate_clip_slot", "duplicate_clip_to_slot", "set_clip_slot_fire_button_state",
                                 "set_device_input_routing",
                                 "apply_cleanup_eq8", "set_track_volume", "set_track_panning", "set_track_send",
                                 "set_track_mute", "set_track_solo", "set_track_arm", "set_track_activator",
                                 "set_track_crossfade_assign", "set_track_panning_mode", "set_track_fold_state",
                                 "set_track_showing_chains", "set_track_collapsed", "set_track_device_insert_mode",
                                 "jump_in_running_session_clip", "stop_track_clips",
                                 "set_song_time", "set_song_record_mode", "set_song_arrangement_overdub",
                                 "set_song_session_automation_record", "set_song_overdub",
                                 "set_song_loop", "set_song_loop_start", "set_song_loop_length", "set_song_metronome",
                                 "set_song_signature", "set_song_exclusive_arm", "set_song_exclusive_solo",
                                 "set_song_groove_amount", "set_song_swing_amount",
                                 "set_song_root_note", "set_song_scale_name", "set_song_scale_mode",
                                 "set_song_clip_trigger_quantization", "set_song_midi_recording_quantization",
                                 "set_song_punch_in", "set_song_punch_out", "set_song_link_enabled",
                                 "set_song_link_start_stop_sync", "set_song_tempo_follower_enabled",
                                 "set_song_nudge_up", "set_song_nudge_down",
                                 "re_enable_automation",
                                 "record_track_send_automation",
                                 "select_track", "select_scene", "select_track_instrument", "select_device",
                                 "select_clip_slot", "fire_clip_slot", "select_parameter",
                                 "show_view", "focus_view", "hide_view",
                                 "scroll_view", "zoom_view", "set_draw_mode", "set_follow_song",
                                 "set_track_output_routing", "set_track_input_routing", "set_track_monitoring_state",
                                 "set_master_volume", "set_master_cue_volume", "set_master_crossfader",
                                 "set_tempo", "tap_tempo", "fire_clip", "stop_clip", "fire_scene", "fire_scene_as_selected",
                                 "set_scene_name", "set_scene_color", "set_scene_tempo", "set_scene_tempo_enabled",
                                 "set_scene_time_signature", "set_scene_time_signature_enabled", "set_scene_fire_button_state",
                                 "start_playback", "stop_playback", "stop_all_clips",
                                 "jump_to_cue_point", "jump_to_next_cue", "jump_to_prev_cue", "set_or_delete_cue",
                                 "undo", "redo", "capture_and_insert_scene", "trigger_session_record", "capture_midi",
                                 "continue_playing", "play_selection", "jump_by", "scrub_by",
                                 "load_browser_item"]:
                # Use a thread-safe approach with a response queue
                response_queue = queue.Queue()
                
                # Define a function to execute on the main thread
                def main_thread_task():
                    try:
                        result = None
                        if command_type == "create_midi_track":
                            index = params.get("index", -1)
                            result = self._create_midi_track(index)
                        elif command_type == "create_audio_track":
                            index = params.get("index", -1)
                            result = self._create_audio_track(index)
                        elif command_type == "create_return_track":
                            result = self._create_return_track()
                        elif command_type == "delete_return_track":
                            track_index = params.get("track_index", 0)
                            result = self._delete_return_track(track_index)
                        elif command_type == "create_scene":
                            index = params.get("index", -1)
                            result = self._create_scene(index)
                        elif command_type == "duplicate_scene":
                            scene_index = params.get("scene_index", 0)
                            result = self._duplicate_scene(scene_index)
                        elif command_type == "delete_scene":
                            scene_index = params.get("scene_index", 0)
                            result = self._delete_scene(scene_index)
                        elif command_type == "duplicate_track":
                            track_index = params.get("track_index", 0)
                            result = self._duplicate_track(track_index)
                        elif command_type == "set_track_name":
                            track_index = params.get("track_index", 0)
                            name = params.get("name", "")
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_name(track_index, name, track_scope)
                        elif command_type == "set_track_color":
                            track_index = params.get("track_index", 0)
                            color = params.get("color", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_color(track_index, color, track_scope)
                        elif command_type == "delete_track":
                            track_index = params.get("track_index", 0)
                            result = self._delete_track(track_index)
                        elif command_type == "delete_device":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            result = self._delete_device(track_index, device_index, track_scope, container_path)
                        elif command_type == "duplicate_device":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            result = self._duplicate_device(track_index, device_index, track_scope, container_path)
                        elif command_type == "insert_device":
                            track_index = params.get("track_index", 0)
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            device_name = params.get("device_name", "")
                            target_index = params.get("target_index", None)
                            result = self._insert_device(
                                track_index,
                                track_scope,
                                container_path,
                                device_name,
                                target_index
                            )
                        elif command_type == "move_device":
                            source_track_index = params.get("source_track_index", 0)
                            source_track_scope = params.get("source_track_scope", "track")
                            source_container_path = params.get("source_container_path", [])
                            source_device_index = params.get("source_device_index", 0)
                            target_track_index = params.get("target_track_index", 0)
                            target_track_scope = params.get("target_track_scope", "track")
                            target_container_path = params.get("target_container_path", [])
                            target_index = params.get("target_index", 0)
                            result = self._move_device(
                                source_track_index,
                                source_track_scope,
                                source_container_path,
                                source_device_index,
                                target_track_index,
                                target_track_scope,
                                target_container_path,
                                target_index
                            )
                        elif command_type == "create_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            length = params.get("length", 4.0)
                            result = self._create_clip(track_index, clip_index, length)
                        elif command_type == "create_take_lane":
                            track_index = params.get("track_index", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._create_take_lane(track_index, track_scope)
                        elif command_type == "create_arrangement_audio_clip":
                            track_index = params.get("track_index", 0)
                            file_path = params.get("file_path", "")
                            position = params.get("position", 0.0)
                            track_scope = params.get("track_scope", "track")
                            result = self._create_arrangement_audio_clip(track_index, file_path, position, track_scope)
                        elif command_type == "create_arrangement_midi_clip":
                            track_index = params.get("track_index", 0)
                            position = params.get("position", 0.0)
                            length = params.get("length", 4.0)
                            track_scope = params.get("track_scope", "track")
                            result = self._create_arrangement_midi_clip(track_index, position, length, track_scope)
                        elif command_type == "add_notes_to_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            notes = params.get("notes", [])
                            result = self._add_notes_to_clip(track_index, clip_index, notes)
                        elif command_type == "set_clip_name":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            name = params.get("name", "")
                            result = self._set_clip_name(track_index, clip_index, name)
                        elif command_type == "set_clip_color":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            color = params.get("color", 0)
                            result = self._set_clip_color(track_index, clip_index, color)
                        elif command_type == "sync_track_media_colors":
                            track_index = params.get("track_index", 0)
                            include_session = params.get("include_session", True)
                            include_arrangement = params.get("include_arrangement", True)
                            result = self._sync_track_media_colors(
                                track_index,
                                include_session,
                                include_arrangement
                            )
                        elif command_type == "sync_all_media_colors":
                            include_session = params.get("include_session", True)
                            include_arrangement = params.get("include_arrangement", True)
                            result = self._sync_all_media_colors(
                                include_session,
                                include_arrangement
                            )
                        elif command_type == "split_arrangement_audio_track_by_clip_name":
                            track_index = params.get("track_index", 0)
                            result = self._split_arrangement_audio_track_by_clip_name(track_index)
                        elif command_type == "delete_clip_in_slot":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._delete_clip_in_slot(track_index, clip_index, track_scope)
                        elif command_type == "duplicate_clip_slot":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._duplicate_clip_slot(track_index, clip_index, track_scope)
                        elif command_type == "duplicate_clip_to_slot":
                            source_track_index = params.get("source_track_index", 0)
                            source_clip_index = params.get("source_clip_index", 0)
                            target_track_index = params.get("target_track_index", 0)
                            target_clip_index = params.get("target_clip_index", 0)
                            result = self._duplicate_clip_to_slot(source_track_index, source_clip_index, target_track_index, target_clip_index)
                        elif command_type == "set_clip_slot_fire_button_state":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            enabled = params.get("enabled", False)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_clip_slot_fire_button_state(track_index, clip_index, enabled, track_scope)
                        elif command_type == "set_device_parameter":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            parameter_name = params.get("parameter_name", "")
                            value = params.get("value", 0)
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            result = self._set_device_parameter(
                                track_index,
                                device_index,
                                parameter_name,
                                value,
                                track_scope,
                                container_path
                            )
                        elif command_type == "set_device_parameters":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            parameter_values = params.get("parameter_values", {})
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            result = self._set_device_parameters(
                                track_index,
                                device_index,
                                parameter_values,
                                track_scope,
                                container_path
                            )
                        elif command_type == "set_device_input_routing":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            routing_name = params.get("routing_name", "")
                            sub_routing_name = params.get("sub_routing_name", "")
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            result = self._set_device_input_routing(
                                track_index,
                                device_index,
                                routing_name,
                                sub_routing_name,
                                track_scope,
                                container_path
                            )
                        elif command_type == "duplicate_clip_to_arrangement":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            destination_time = params.get("destination_time", 0.0)
                            track_scope = params.get("track_scope", "track")
                            result = self._duplicate_clip_to_arrangement(
                                track_index,
                                clip_index,
                                destination_time,
                                track_scope
                            )
                        elif command_type == "clear_clip_automation":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            arrangement = params.get("arrangement", False)
                            arrangement_clip_index = params.get("arrangement_clip_index", 0)
                            device_index = params.get("device_index", 0)
                            parameter_name = params.get("parameter_name", "")
                            parameter_source = params.get("parameter_source", "device")
                            send_index = params.get("send_index", 0)
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            result = self._clear_clip_automation(
                                track_index,
                                clip_index,
                                arrangement,
                                arrangement_clip_index,
                                device_index,
                                parameter_name,
                                parameter_source,
                                send_index,
                                track_scope,
                                container_path
                            )
                        elif command_type == "delete_clip_automation_events":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            arrangement = params.get("arrangement", False)
                            arrangement_clip_index = params.get("arrangement_clip_index", 0)
                            device_index = params.get("device_index", 0)
                            parameter_name = params.get("parameter_name", "")
                            parameter_source = params.get("parameter_source", "device")
                            send_index = params.get("send_index", 0)
                            start_time = params.get("start_time", 0.0)
                            end_time = params.get("end_time", 0.0)
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            result = self._delete_clip_automation_events(
                                track_index,
                                clip_index,
                                arrangement,
                                arrangement_clip_index,
                                device_index,
                                parameter_name,
                                parameter_source,
                                send_index,
                                start_time,
                                end_time,
                                track_scope,
                                container_path
                            )
                        elif command_type == "set_clip_automation_steps":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            arrangement = params.get("arrangement", False)
                            arrangement_clip_index = params.get("arrangement_clip_index", 0)
                            device_index = params.get("device_index", 0)
                            parameter_name = params.get("parameter_name", "")
                            parameter_source = params.get("parameter_source", "device")
                            send_index = params.get("send_index", 0)
                            steps = params.get("steps", [])
                            clear_existing = params.get("clear_existing", True)
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            result = self._set_clip_automation_steps(
                                track_index,
                                clip_index,
                                arrangement,
                                arrangement_clip_index,
                                device_index,
                                parameter_name,
                                parameter_source,
                                send_index,
                                steps,
                                clear_existing,
                                track_scope,
                                container_path
                            )
                        elif command_type == "apply_cleanup_eq8":
                            track_index = params.get("track_index", 0)
                            low_cut_hz = params.get("low_cut_hz", 40.0)
                            high_cut_hz = params.get("high_cut_hz", 18000.0)
                            load_if_missing = params.get("load_if_missing", True)
                            result = self._apply_cleanup_eq8(
                                track_index,
                                low_cut_hz,
                                high_cut_hz,
                                load_if_missing
                            )
                        elif command_type == "set_track_volume":
                            track_index = params.get("track_index", 0)
                            value = params.get("value", 0.85)
                            result = self._set_track_volume(track_index, value)
                        elif command_type == "set_track_panning":
                            track_index = params.get("track_index", 0)
                            value = params.get("value", 0.0)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_panning(track_index, value, track_scope)
                        elif command_type == "set_track_send":
                            track_index = params.get("track_index", 0)
                            send_index = params.get("send_index", 0)
                            value = params.get("value", 0.0)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_send(track_index, send_index, value, track_scope)
                        elif command_type == "set_track_mute":
                            track_index = params.get("track_index", 0)
                            enabled = params.get("enabled", False)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_mute(track_index, enabled, track_scope)
                        elif command_type == "set_track_solo":
                            track_index = params.get("track_index", 0)
                            enabled = params.get("enabled", False)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_solo(track_index, enabled, track_scope)
                        elif command_type == "set_track_arm":
                            track_index = params.get("track_index", 0)
                            enabled = params.get("enabled", False)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_arm(track_index, enabled, track_scope)
                        elif command_type == "set_track_activator":
                            track_index = params.get("track_index", 0)
                            enabled = params.get("enabled", True)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_activator(track_index, enabled, track_scope)
                        elif command_type == "set_track_crossfade_assign":
                            track_index = params.get("track_index", 0)
                            value = params.get("value", 1)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_crossfade_assign(track_index, value, track_scope)
                        elif command_type == "set_track_panning_mode":
                            track_index = params.get("track_index", 0)
                            value = params.get("value", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_panning_mode(track_index, value, track_scope)
                        elif command_type == "set_track_fold_state":
                            track_index = params.get("track_index", 0)
                            value = params.get("value", False)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_fold_state(track_index, value, track_scope)
                        elif command_type == "set_track_showing_chains":
                            track_index = params.get("track_index", 0)
                            enabled = params.get("enabled", False)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_showing_chains(track_index, enabled, track_scope)
                        elif command_type == "set_track_collapsed":
                            track_index = params.get("track_index", 0)
                            enabled = params.get("enabled", False)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_collapsed(track_index, enabled, track_scope)
                        elif command_type == "set_track_device_insert_mode":
                            track_index = params.get("track_index", 0)
                            enabled = params.get("enabled", False)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_device_insert_mode(track_index, enabled, track_scope)
                        elif command_type == "jump_in_running_session_clip":
                            track_index = params.get("track_index", 0)
                            beats = params.get("beats", 0.0)
                            track_scope = params.get("track_scope", "track")
                            result = self._jump_in_running_session_clip(track_index, beats, track_scope)
                        elif command_type == "stop_track_clips":
                            track_index = params.get("track_index", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._stop_track_clips(track_index, track_scope)
                        elif command_type == "set_song_time":
                            result = self._set_song_time(params.get("time", 0.0))
                        elif command_type == "set_song_record_mode":
                            result = self._set_song_record_mode(params.get("enabled", False))
                        elif command_type == "set_song_arrangement_overdub":
                            result = self._set_song_arrangement_overdub(params.get("enabled", False))
                        elif command_type == "set_song_session_automation_record":
                            result = self._set_song_session_automation_record(params.get("enabled", False))
                        elif command_type == "set_song_overdub":
                            result = self._set_song_overdub(params.get("enabled", False))
                        elif command_type == "set_song_loop":
                            result = self._set_song_loop(
                                params.get("enabled", False),
                                params.get("start_time", None),
                                params.get("length", None)
                            )
                        elif command_type == "set_song_loop_start":
                            result = self._set_song_loop_start(params.get("value", 0.0))
                        elif command_type == "set_song_loop_length":
                            result = self._set_song_loop_length(params.get("value", 4.0))
                        elif command_type == "set_song_metronome":
                            result = self._set_song_metronome(params.get("enabled", False))
                        elif command_type == "set_song_signature":
                            result = self._set_song_signature(
                                params.get("numerator", 4),
                                params.get("denominator", 4)
                            )
                        elif command_type == "set_song_exclusive_arm":
                            result = self._set_song_exclusive_arm(params.get("enabled", True))
                        elif command_type == "set_song_exclusive_solo":
                            result = self._set_song_exclusive_solo(params.get("enabled", True))
                        elif command_type == "set_song_groove_amount":
                            result = self._set_song_groove_amount(params.get("value", 0.0))
                        elif command_type == "set_song_swing_amount":
                            result = self._set_song_swing_amount(params.get("value", 0.0))
                        elif command_type == "set_song_root_note":
                            result = self._set_song_root_note(params.get("value", 0))
                        elif command_type == "set_song_scale_name":
                            result = self._set_song_scale_name(params.get("value", "Major"))
                        elif command_type == "set_song_scale_mode":
                            result = self._set_song_scale_mode(params.get("enabled", True))
                        elif command_type == "set_song_clip_trigger_quantization":
                            result = self._set_song_clip_trigger_quantization(params.get("value", 0))
                        elif command_type == "set_song_midi_recording_quantization":
                            result = self._set_song_midi_recording_quantization(params.get("value", 0))
                        elif command_type == "set_song_punch_in":
                            result = self._set_song_punch_in(params.get("enabled", False))
                        elif command_type == "set_song_punch_out":
                            result = self._set_song_punch_out(params.get("enabled", False))
                        elif command_type == "set_song_link_enabled":
                            result = self._set_song_link_enabled(params.get("enabled", False))
                        elif command_type == "set_song_link_start_stop_sync":
                            result = self._set_song_link_start_stop_sync(params.get("enabled", False))
                        elif command_type == "set_song_tempo_follower_enabled":
                            result = self._set_song_tempo_follower_enabled(params.get("enabled", False))
                        elif command_type == "set_song_nudge_up":
                            result = self._set_song_nudge_up(params.get("enabled", False))
                        elif command_type == "set_song_nudge_down":
                            result = self._set_song_nudge_down(params.get("enabled", False))
                        elif command_type == "re_enable_automation":
                            result = self._re_enable_automation()
                        elif command_type == "record_track_send_automation":
                            track_index = params.get("track_index", 0)
                            send_index = params.get("send_index", 0)
                            points = params.get("points", [])
                            track_scope = params.get("track_scope", "track")
                            pre_roll_beats = params.get("pre_roll_beats", 0.125)
                            settle_seconds = params.get("settle_seconds", 0.03)
                            poll_interval_seconds = params.get("poll_interval_seconds", 0.01)
                            max_segment_gap_beats = params.get("max_segment_gap_beats", 8.0)
                            restore_transport = params.get("restore_transport", True)
                            result = self._record_track_send_automation(
                                track_index,
                                send_index,
                                points,
                                track_scope,
                                pre_roll_beats,
                                settle_seconds,
                                poll_interval_seconds,
                                max_segment_gap_beats,
                                restore_transport
                            )
                        elif command_type == "select_track":
                            track_index = params.get("track_index", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._select_track(track_index, track_scope)
                        elif command_type == "select_scene":
                            scene_index = params.get("scene_index", 0)
                            result = self._select_scene(scene_index)
                        elif command_type == "select_track_instrument":
                            track_index = params.get("track_index", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._select_track_instrument(track_index, track_scope)
                        elif command_type == "select_device":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            result = self._select_device(track_index, device_index, track_scope, container_path)
                        elif command_type == "select_clip_slot":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._select_clip_slot(track_index, clip_index, track_scope)
                        elif command_type == "fire_clip_slot":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._fire_clip_slot(track_index, clip_index, track_scope)
                        elif command_type == "select_parameter":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            parameter_name = params.get("parameter_name", "")
                            track_scope = params.get("track_scope", "track")
                            container_path = params.get("container_path", [])
                            parameter_source = params.get("parameter_source", "device")
                            send_index = params.get("send_index", 0)
                            result = self._select_parameter(
                                track_index,
                                device_index,
                                parameter_name,
                                track_scope,
                                container_path,
                                parameter_source,
                                send_index
                            )
                        elif command_type == "show_view":
                            view_name = params.get("view_name", "")
                            result = self._show_view(view_name)
                        elif command_type == "focus_view":
                            view_name = params.get("view_name", "")
                            result = self._focus_view(view_name)
                        elif command_type == "hide_view":
                            view_name = params.get("view_name", "")
                            result = self._hide_view(view_name)
                        elif command_type == "scroll_view":
                            direction = params.get("direction", 0)
                            view_name = params.get("view_name", "")
                            modifier_pressed = params.get("modifier_pressed", False)
                            amount = params.get("amount", 1)
                            result = self._scroll_view(direction, view_name, modifier_pressed, amount)
                        elif command_type == "zoom_view":
                            direction = params.get("direction", 0)
                            view_name = params.get("view_name", "")
                            modifier_pressed = params.get("modifier_pressed", False)
                            amount = params.get("amount", 1)
                            result = self._zoom_view(direction, view_name, modifier_pressed, amount)
                        elif command_type == "set_draw_mode":
                            result = self._set_draw_mode(params.get("enabled", False))
                        elif command_type == "set_follow_song":
                            result = self._set_follow_song(params.get("enabled", False))
                        elif command_type == "set_track_output_routing":
                            track_index = params.get("track_index", 0)
                            routing_name = params.get("routing_name", "")
                            sub_routing_name = params.get("sub_routing_name", "")
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_output_routing(track_index, routing_name, sub_routing_name, track_scope)
                        elif command_type == "set_track_input_routing":
                            track_index = params.get("track_index", 0)
                            routing_name = params.get("routing_name", "")
                            sub_routing_name = params.get("sub_routing_name", "")
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_input_routing(track_index, routing_name, sub_routing_name, track_scope)
                        elif command_type == "set_track_monitoring_state":
                            track_index = params.get("track_index", 0)
                            state = params.get("state", 0)
                            track_scope = params.get("track_scope", "track")
                            result = self._set_track_monitoring_state(track_index, state, track_scope)
                        elif command_type == "set_master_volume":
                            value = params.get("value", 0.85)
                            result = self._set_master_volume(value)
                        elif command_type == "set_master_cue_volume":
                            value = params.get("value", 0.85)
                            result = self._set_master_cue_volume(value)
                        elif command_type == "set_master_crossfader":
                            value = params.get("value", 0.5)
                            result = self._set_master_crossfader(value)
                        elif command_type == "set_tempo":
                            tempo = params.get("tempo", 120.0)
                            result = self._set_tempo(tempo)
                        elif command_type == "tap_tempo":
                            result = self._tap_tempo()
                        elif command_type == "fire_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._fire_clip(track_index, clip_index)
                        elif command_type == "stop_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._stop_clip(track_index, clip_index)
                        elif command_type == "fire_scene":
                            scene_index = params.get("scene_index", 0)
                            result = self._fire_scene(scene_index)
                        elif command_type == "fire_scene_as_selected":
                            scene_index = params.get("scene_index", 0)
                            result = self._fire_scene_as_selected(scene_index)
                        elif command_type == "set_scene_name":
                            scene_index = params.get("scene_index", 0)
                            name = params.get("name", "")
                            result = self._set_scene_name(scene_index, name)
                        elif command_type == "set_scene_color":
                            scene_index = params.get("scene_index", 0)
                            color = params.get("color", 0)
                            result = self._set_scene_color(scene_index, color)
                        elif command_type == "set_scene_tempo":
                            scene_index = params.get("scene_index", 0)
                            value = params.get("value", 120.0)
                            result = self._set_scene_tempo(scene_index, value)
                        elif command_type == "set_scene_tempo_enabled":
                            scene_index = params.get("scene_index", 0)
                            enabled = params.get("enabled", False)
                            result = self._set_scene_tempo_enabled(scene_index, enabled)
                        elif command_type == "set_scene_time_signature":
                            scene_index = params.get("scene_index", 0)
                            numerator = params.get("numerator", 4)
                            denominator = params.get("denominator", 4)
                            result = self._set_scene_time_signature(scene_index, numerator, denominator)
                        elif command_type == "set_scene_time_signature_enabled":
                            scene_index = params.get("scene_index", 0)
                            enabled = params.get("enabled", False)
                            result = self._set_scene_time_signature_enabled(scene_index, enabled)
                        elif command_type == "set_scene_fire_button_state":
                            scene_index = params.get("scene_index", 0)
                            enabled = params.get("enabled", False)
                            result = self._set_scene_fire_button_state(scene_index, enabled)
                        elif command_type == "start_playback":
                            result = self._start_playback()
                        elif command_type == "stop_playback":
                            result = self._stop_playback()
                        elif command_type == "stop_all_clips":
                            result = self._stop_all_clips()
                        elif command_type == "jump_to_cue_point":
                            cue_index = params.get("cue_index", 0)
                            result = self._jump_to_cue_point(cue_index)
                        elif command_type == "jump_to_next_cue":
                            result = self._jump_to_next_cue()
                        elif command_type == "jump_to_prev_cue":
                            result = self._jump_to_prev_cue()
                        elif command_type == "set_or_delete_cue":
                            result = self._set_or_delete_cue()
                        elif command_type == "undo":
                            result = self._undo()
                        elif command_type == "redo":
                            result = self._redo()
                        elif command_type == "capture_and_insert_scene":
                            result = self._capture_and_insert_scene()
                        elif command_type == "trigger_session_record":
                            result = self._trigger_session_record()
                        elif command_type == "capture_midi":
                            result = self._capture_midi()
                        elif command_type == "continue_playing":
                            result = self._continue_playing()
                        elif command_type == "play_selection":
                            result = self._play_selection()
                        elif command_type == "jump_by":
                            result = self._jump_by(params.get("beats", 0.0))
                        elif command_type == "scrub_by":
                            result = self._scrub_by(params.get("beats", 0.0))
                        elif command_type == "load_instrument_or_effect":
                            track_index = params.get("track_index", 0)
                            uri = params.get("uri", "")
                            result = self._load_instrument_or_effect(track_index, uri)
                        elif command_type == "load_browser_item":
                            track_index = params.get("track_index", 0)
                            item_uri = params.get("item_uri", "")
                            selected_device_index = params.get("selected_device_index", None)
                            insert_mode = params.get("insert_mode", None)
                            track_scope = params.get("track_scope", "track")
                            result = self._load_browser_item(
                                track_index,
                                item_uri,
                                track_scope,
                                selected_device_index,
                                insert_mode
                            )
                        
                        # Put the result in the queue
                        response_queue.put({"status": "success", "result": result})
                    except Exception as e:
                        self.log_message("Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put({"status": "error", "message": str(e)})
                
                # Schedule the task to run on the main thread
                try:
                    self.schedule_message(0, main_thread_task)
                except AssertionError:
                    # If we're already on the main thread, execute directly
                    main_thread_task()
                
                # Wait for the response with a timeout
                try:
                    timeout_seconds = 120.0 if command_type == "record_track_send_automation" else 10.0
                    task_response = response_queue.get(timeout=timeout_seconds)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get("message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = "Timeout waiting for operation to complete"
            elif command_type == "get_browser_item":
                uri = params.get("uri", None)
                path = params.get("path", None)
                response["result"] = self._get_browser_item(uri, path)
            elif command_type == "get_browser_categories":
                category_type = params.get("category_type", "all")
                response["result"] = self._get_browser_categories(category_type)
            elif command_type == "get_browser_items":
                path = params.get("path", "")
                item_type = params.get("item_type", "all")
                response["result"] = self._get_browser_items(path, item_type)
            # Add the new browser commands
            elif command_type == "get_browser_tree":
                category_type = params.get("category_type", "all")
                response["result"] = self.get_browser_tree(category_type)
            elif command_type == "get_browser_items_at_path":
                path = params.get("path", "")
                response["result"] = self.get_browser_items_at_path(path)
            else:
                response["status"] = "error"
                response["message"] = "Unknown command: " + command_type
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            response["status"] = "error"
            response["message"] = str(e)
        
        return self._make_json_safe(response)
    
    # Command implementations
    
    def _get_session_info(self):
        """Get information about the current session"""
        try:
            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                }
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise

    def _get_level_snapshot(self):
        """Get current meter levels for the master and all tracks."""
        try:
            master = self._song.master_track
            master_result = {
                "name": "Master",
                "volume": master.mixer_device.volume.value,
                "output_meter_left": getattr(master, "output_meter_left", 0.0),
                "output_meter_right": getattr(master, "output_meter_right", 0.0),
                "output_meter_level": getattr(master, "output_meter_level", 0.0)
            }

            tracks = []
            for track_index, track in enumerate(self._song.tracks):
                try:
                    is_group_track = track.is_foldable
                except Exception:
                    is_group_track = False

                tracks.append({
                    "index": track_index,
                    "name": track.name,
                    "is_group_track": is_group_track,
                    "mute": track.mute,
                    "solo": track.solo,
                    "volume": track.mixer_device.volume.value,
                    "output_meter_left": getattr(track, "output_meter_left", 0.0),
                    "output_meter_right": getattr(track, "output_meter_right", 0.0),
                    "output_meter_level": getattr(track, "output_meter_level", 0.0)
                })

            return {
                "is_playing": self._song.is_playing,
                "master": master_result,
                "tracks": tracks
            }
        except Exception as e:
            self.log_message("Error getting level snapshot: " + str(e))
            raise

    def _get_supported_commands(self):
        """Return the bridge version and currently declared command names."""
        commands = []
        try:
            import re
            source_path = __file__
            with open(source_path, 'r') as handle:
                source = handle.read()
            commands = sorted(set(re.findall(r'command_type == \"([^\"]+)\"', source)))
        except Exception:
            commands = []

        return {
            "bridge_version": BRIDGE_VERSION,
            "port": DEFAULT_PORT,
            "count": len(commands),
            "commands": commands
        }
    
    def _resolve_track(self, track_index, track_scope="track"):
        """Resolve a track-like object from the main tracks list or the master track."""
        if track_scope == "master" or track_index == -1:
            return self._song.master_track

        if track_scope == "return":
            if track_index < 0 or track_index >= len(self._song.return_tracks):
                raise IndexError("Return track index out of range")
            return self._song.return_tracks[track_index]

        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")

        return self._song.tracks[track_index]

    def _serialize_track_sends(self, track):
        """Serialize mixer send values for a track-like object."""
        sends = []
        mixer_device = getattr(track, "mixer_device", None)
        if mixer_device is None or not hasattr(mixer_device, "sends"):
            return sends

        for send_index, send_parameter in enumerate(list(mixer_device.sends)):
            send_name = ""
            if send_index < len(self._song.return_tracks):
                try:
                    send_name = self._song.return_tracks[send_index].name
                except Exception:
                    send_name = ""
            sends.append({
                "index": send_index,
                "name": send_name,
                "value": send_parameter.value,
                "min": getattr(send_parameter, "min", 0.0),
                "max": getattr(send_parameter, "max", 1.0),
                "display_value": getattr(send_parameter, "display_value", None),
                "automation_state": getattr(send_parameter, "automation_state", None)
            })
        return sends

    def _get_song_state(self):
        """Return transport and automation-record telemetry for the current song."""
        return {
            "tempo": self._song.tempo,
            "is_playing": self._song.is_playing,
            "current_song_time": self._song.current_song_time,
            "record_mode": getattr(self._song, "record_mode", False),
            "arrangement_overdub": getattr(self._song, "arrangement_overdub", False),
            "session_automation_record": getattr(self._song, "session_automation_record", False),
            "overdub": getattr(self._song, "overdub", False),
            "back_to_arranger": getattr(self._song, "back_to_arranger", False)
        }

    def _find_tracklike_parent(self, obj):
        """Walk canonical parents until we reach a track-like object."""
        current = obj
        for _ in range(16):
            if current is None:
                return None
            try:
                if current == self._song.master_track:
                    return current
            except Exception:
                pass
            try:
                for candidate in list(self._song.return_tracks):
                    if current == candidate:
                        return current
            except Exception:
                pass
            try:
                for candidate in list(self._song.tracks):
                    if current == candidate:
                        return current
            except Exception:
                pass
            try:
                current = current.canonical_parent
            except Exception:
                return None
        return None

    def _resolve_device_reference(self, device):
        """Return a readable reference for a device-like object."""
        if device is None:
            return None

        track = self._find_tracklike_parent(device)
        track_ref = self._resolve_track_reference(track)
        device_index = None
        try:
            for index, candidate in enumerate(list(track.devices)):
                if candidate == device:
                    device_index = index
                    break
        except Exception:
            pass

        return {
            "track": track_ref,
            "device_index": device_index,
            "name": getattr(device, "name", None),
            "class_name": getattr(device, "class_name", None)
        }

    def _resolve_scene(self, scene_index):
        """Resolve a scene by index."""
        scenes = list(self._song.scenes)
        scene_index = int(scene_index)
        if scene_index < 0 or scene_index >= len(scenes):
            raise IndexError("Scene index out of range")
        return scenes[scene_index]

    def _resolve_scene_reference(self, scene):
        """Map a scene object back to index/name."""
        if scene is None:
            return {
                "scene_index": None,
                "scene_name": None
            }

        for index, candidate in enumerate(list(self._song.scenes)):
            if candidate == scene:
                return {
                    "scene_index": index,
                    "scene_name": getattr(candidate, "name", "")
                }

        return {
            "scene_index": None,
            "scene_name": getattr(scene, "name", None)
        }

    def _resolve_clip_slot_reference(self, clip_slot):
        """Map a clip slot object back to track/scene coordinates."""
        if clip_slot is None:
            return None

        for track_index, track in enumerate(list(self._song.tracks)):
            try:
                slots = list(track.clip_slots)
            except Exception:
                slots = []
            for slot_index, candidate in enumerate(slots):
                if candidate == clip_slot:
                    clip_name = None
                    try:
                        if candidate.has_clip:
                            clip_name = candidate.clip.name
                    except Exception:
                        pass
                    return {
                        "track": self._resolve_track_reference(track),
                        "clip_index": slot_index,
                        "clip_name": clip_name
                    }
        return None

    def _resolve_clip_slot(self, track_index, clip_index, track_scope="track"):
        """Resolve a clip slot on a regular track-like object."""
        track = self._resolve_track(track_index, track_scope)
        clip_slots = list(getattr(track, "clip_slots", []))
        clip_index = int(clip_index)
        if clip_index < 0 or clip_index >= len(clip_slots):
            raise IndexError("Clip slot index out of range")
        return track, clip_slots[clip_index]

    def _describe_scene(self, scene, scene_index):
        """Return readable telemetry for a scene."""
        clip_slots = []
        try:
            scene_clip_slots = list(scene.clip_slots)
        except Exception:
            scene_clip_slots = []

        for track_index, slot in enumerate(scene_clip_slots):
            try:
                has_clip = bool(slot.has_clip)
            except Exception:
                has_clip = False
            clip_name = None
            clip_color = None
            if has_clip:
                try:
                    clip_name = slot.clip.name
                except Exception:
                    pass
                try:
                    clip_color = slot.clip.color
                except Exception:
                    pass
            clip_slots.append({
                "track_index": track_index,
                "track_name": getattr(self._song.tracks[track_index], "name", ""),
                "has_clip": has_clip,
                "clip_name": clip_name,
                "clip_color": clip_color
            })

        return {
            "scene_index": scene_index,
            "name": getattr(scene, "name", ""),
            "color": getattr(scene, "color", None),
            "is_empty": bool(getattr(scene, "is_empty", False)),
            "tempo": getattr(scene, "tempo", None),
            "tempo_enabled": getattr(scene, "tempo_enabled", None),
            "time_signature_numerator": getattr(scene, "time_signature_numerator", None),
            "time_signature_denominator": getattr(scene, "time_signature_denominator", None),
            "time_signature_enabled": getattr(scene, "time_signature_enabled", None),
            "clip_slots": clip_slots
        }

    def _describe_cue_point(self, cue_point, cue_index):
        """Return readable telemetry for a cue point."""
        result = {
            "cue_index": cue_index,
            "name": getattr(cue_point, "name", ""),
            "time": getattr(cue_point, "time", None)
        }
        try:
            result["is_selected"] = bool(cue_point == self._song.view.selected_scene)
        except Exception:
            pass
        return result

    def _serialize_clip_slot(self, slot, track_index, clip_index):
        """Serialize a clip slot with current state."""
        clip = None
        has_clip = False
        try:
            has_clip = bool(slot.has_clip)
        except Exception:
            has_clip = False

        if has_clip:
            try:
                clip = {
                    "name": slot.clip.name,
                    "length": slot.clip.length,
                    "color": slot.clip.color,
                    "is_audio_clip": getattr(slot.clip, "is_audio_clip", None),
                    "is_midi_clip": getattr(slot.clip, "is_midi_clip", None),
                    "is_session_clip": getattr(slot.clip, "is_session_clip", None),
                    "is_playing": getattr(slot.clip, "is_playing", None),
                    "is_recording": getattr(slot.clip, "is_recording", None),
                    "is_triggered": getattr(slot.clip, "is_triggered", None)
                }
            except Exception:
                clip = None

        return {
            "track_index": track_index,
            "clip_index": clip_index,
            "has_clip": has_clip,
            "controls_other_clips": getattr(slot, "controls_other_clips", None),
            "has_stop_button": getattr(slot, "has_stop_button", None),
            "is_playing": getattr(slot, "is_playing", None),
            "is_recording": getattr(slot, "is_recording", None),
            "is_triggered": getattr(slot, "is_triggered", None),
            "playing_status": getattr(slot, "playing_status", None),
            "clip": clip
        }

    def _resolve_track_reference(self, track):
        """Map a track object back to a readable scope/index tuple."""
        if track is None:
            return {
                "track_scope": None,
                "track_index": None,
                "track_name": None
            }

        try:
            if track == self._song.master_track:
                return {
                    "track_scope": "master",
                    "track_index": 0,
                    "track_name": getattr(track, "name", "Master")
                }
        except Exception:
            pass

        for index, candidate in enumerate(list(self._song.return_tracks)):
            if track == candidate:
                return {
                    "track_scope": "return",
                    "track_index": index,
                    "track_name": getattr(candidate, "name", "")
                }

        for index, candidate in enumerate(list(self._song.tracks)):
            if track == candidate:
                return {
                    "track_scope": "track",
                    "track_index": index,
                    "track_name": getattr(candidate, "name", "")
                }

        return {
            "track_scope": "unknown",
            "track_index": None,
            "track_name": getattr(track, "name", None)
        }

    def _describe_parameter(self, parameter):
        """Return a readable summary for a parameter-like object."""
        if parameter is None:
            return None

        names = []
        for attr_name in ["name", "original_name"]:
            try:
                value = getattr(parameter, attr_name)
            except Exception:
                value = None
            if value and value not in names:
                names.append(value)

        parent_name = None
        parent_type = None
        parent_track = None
        try:
            parent = parameter.canonical_parent
            parent_name = getattr(parent, "name", None)
            parent_type = parent.__class__.__name__
            maybe_track = getattr(parent, "canonical_parent", None)
            if maybe_track is not None:
                parent_track = self._resolve_track_reference(maybe_track)
        except Exception:
            pass

        try:
            value = parameter.value
        except Exception:
            value = None

        try:
            display_value = parameter.display_value
        except Exception:
            display_value = None

        try:
            automation_state = parameter.automation_state
        except Exception:
            automation_state = None

        return {
            "name": names[0] if names else None,
            "names": names,
            "value": value,
            "display_value": display_value,
            "automation_state": automation_state,
            "parent_name": parent_name,
            "parent_type": parent_type,
            "parent_track": parent_track
        }

    def _get_song_overview(self):
        """Return broader song/session telemetry exposed by the Live API."""
        overview = self._get_song_state()
        overview.update({
            "name": getattr(self._song, "name", None),
            "file_path": getattr(self._song, "file_path", None),
            "signature_numerator": getattr(self._song, "signature_numerator", None),
            "signature_denominator": getattr(self._song, "signature_denominator", None),
            "clip_trigger_quantization": getattr(self._song, "clip_trigger_quantization", None),
            "midi_recording_quantization": getattr(self._song, "midi_recording_quantization", None),
            "count_in_duration": getattr(self._song, "count_in_duration", None),
            "metronome": getattr(self._song, "metronome", None),
            "exclusive_arm": getattr(self._song, "exclusive_arm", None),
            "exclusive_solo": getattr(self._song, "exclusive_solo", None),
            "groove_amount": getattr(self._song, "groove_amount", None),
            "swing_amount": getattr(self._song, "swing_amount", None),
            "can_capture_midi": getattr(self._song, "can_capture_midi", None),
            "can_jump_to_next_cue": getattr(self._song, "can_jump_to_next_cue", None),
            "can_jump_to_prev_cue": getattr(self._song, "can_jump_to_prev_cue", None),
            "can_undo": getattr(self._song, "can_undo", None),
            "can_redo": getattr(self._song, "can_redo", None),
            "loop": getattr(self._song, "loop", None),
            "loop_start": getattr(self._song, "loop_start", None),
            "loop_length": getattr(self._song, "loop_length", None),
            "song_length": getattr(self._song, "song_length", None),
            "start_time": getattr(self._song, "start_time", None),
            "punch_in": getattr(self._song, "punch_in", None),
            "punch_out": getattr(self._song, "punch_out", None),
            "re_enable_automation_enabled": getattr(self._song, "re_enable_automation_enabled", None),
            "session_record": getattr(self._song, "session_record", None),
            "session_record_status": getattr(self._song, "session_record_status", None),
            "is_counting_in": getattr(self._song, "is_counting_in", None),
            "is_ableton_link_enabled": getattr(self._song, "is_ableton_link_enabled", None),
            "is_ableton_link_start_stop_sync_enabled": getattr(self._song, "is_ableton_link_start_stop_sync_enabled", None),
            "tempo_follower_enabled": getattr(self._song, "tempo_follower_enabled", None),
            "root_note": getattr(self._song, "root_note", None),
            "scale_name": getattr(self._song, "scale_name", None),
            "scale_mode": getattr(self._song, "scale_mode", None),
            "scale_intervals": list(getattr(self._song, "scale_intervals", [])),
            "tuning_system": getattr(self._song, "tuning_system", None),
            "track_count": len(self._song.tracks),
            "visible_track_count": len(getattr(self._song, "visible_tracks", [])),
            "return_track_count": len(self._song.return_tracks),
            "scene_count": len(self._song.scenes),
            "cue_point_count": len(getattr(self._song, "cue_points", [])),
            "appointed_device": self._resolve_device_reference(getattr(self._song, "appointed_device", None))
        })
        return overview

    def _get_scenes(self):
        """Return all scenes with lightweight telemetry."""
        scenes = []
        for scene_index, scene in enumerate(list(self._song.scenes)):
            scenes.append({
                "scene_index": scene_index,
                "name": getattr(scene, "name", ""),
                "color": getattr(scene, "color", None),
                "is_empty": bool(getattr(scene, "is_empty", False))
            })
        return {
            "count": len(scenes),
            "selected_scene": self._resolve_scene_reference(getattr(self._song.view, "selected_scene", None)),
            "scenes": scenes
        }

    def _get_scene_info(self, scene_index):
        """Return detailed scene telemetry, including clip slots across tracks."""
        scene = self._resolve_scene(scene_index)
        return self._describe_scene(scene, int(scene_index))

    def _get_cue_points(self):
        """Return current cue points."""
        cue_points = []
        for cue_index, cue_point in enumerate(list(getattr(self._song, "cue_points", []))):
            cue_points.append(self._describe_cue_point(cue_point, cue_index))
        return {
            "count": len(cue_points),
            "can_jump_to_next_cue": getattr(self._song, "can_jump_to_next_cue", None),
            "can_jump_to_prev_cue": getattr(self._song, "can_jump_to_prev_cue", None),
            "cue_points": cue_points
        }

    def _get_visible_tracks(self):
        """Return tracks that are currently visible in Live."""
        tracks = []
        for index, track in enumerate(list(getattr(self._song, "visible_tracks", []))):
            tracks.append({
                "visible_index": index,
                "track": self._resolve_track_reference(track),
                "is_part_of_selection": getattr(track, "is_part_of_selection", None)
            })
        return {
            "count": len(tracks),
            "tracks": tracks
        }

    def _get_view_state(self):
        """Return Live view and selection state relevant to arrangement automation."""
        app_view = self.application().view
        song_view = self._song.view

        visible_views = {}
        for view_name in ["Browser", "Arranger", "Session", "Detail", "Detail/Clip", "Detail/DeviceChain"]:
            try:
                visible_views[view_name] = bool(app_view.is_view_visible(view_name))
            except Exception:
                visible_views[view_name] = None

        try:
            selected_track = self._resolve_track_reference(song_view.selected_track)
        except Exception:
            selected_track = self._resolve_track_reference(None)

        try:
            selected_parameter = self._describe_parameter(song_view.selected_parameter)
        except Exception:
            selected_parameter = None

        try:
            selected_scene = self._resolve_scene_reference(song_view.selected_scene)
        except Exception:
            selected_scene = self._resolve_scene_reference(None)

        try:
            highlighted_clip_slot = self._resolve_clip_slot_reference(song_view.highlighted_clip_slot)
        except Exception:
            highlighted_clip_slot = None

        try:
            selected_chain = getattr(song_view.selected_chain, "name", None)
        except Exception:
            selected_chain = None

        detail_clip = None
        try:
            clip = song_view.detail_clip
            if clip is not None:
                detail_clip = {
                    "name": clip.name,
                    "position": getattr(clip, "position", None),
                    "length": getattr(clip, "length", None),
                    "is_session_clip": getattr(clip, "is_session_clip", None),
                    "is_arrangement_clip": getattr(clip, "is_arrangement_clip", None)
                }
        except Exception:
            detail_clip = None

        appointed_device = self._resolve_device_reference(getattr(self._song, "appointed_device", None))

        return {
            "focused_document_view": getattr(app_view, "focused_document_view", None),
            "browse_mode": getattr(app_view, "browse_mode", None),
            "visible_views": visible_views,
            "draw_mode": getattr(song_view, "draw_mode", None),
            "follow_song": getattr(song_view, "follow_song", None),
            "selected_track": selected_track,
            "selected_scene": selected_scene,
            "highlighted_clip_slot": highlighted_clip_slot,
            "selected_chain": selected_chain,
            "selected_parameter": selected_parameter,
            "appointed_device": appointed_device,
            "detail_clip": detail_clip
        }

    def _get_arrangement_clips(self, track_index, track_scope="track"):
        """Return arrangement clip timing metadata for a track."""
        track = self._resolve_track(track_index, track_scope)
        result = []
        try:
            arrangement_clips = list(track.arrangement_clips)
        except Exception:
            arrangement_clips = []

        for arrangement_clip_index, clip in enumerate(arrangement_clips):
            try:
                position = float(clip.position)
            except Exception:
                position = None
            try:
                length = float(clip.length)
            except Exception:
                length = None

            result.append({
                "arrangement_clip_index": arrangement_clip_index,
                "name": clip.name,
                "position": position,
                "length": length,
                "end_time": (position + length) if position is not None and length is not None else None,
                "start_marker": getattr(clip, "start_marker", None),
                "end_marker": getattr(clip, "end_marker", None),
                "loop_start": getattr(clip, "loop_start", None),
                "loop_end": getattr(clip, "loop_end", None),
                "is_audio_clip": getattr(clip, "is_audio_clip", None),
                "is_midi_clip": getattr(clip, "is_midi_clip", None),
                "color": getattr(clip, "color", None)
            })

        return {
            "track_scope": track_scope,
            "track_index": track_index,
            "track_name": getattr(track, "name", ""),
            "clip_count": len(result),
            "clips": result
        }

    def _resolve_send_parameter(self, track_index, send_index, track_scope="track"):
        """Resolve a send parameter for a track-like object."""
        track = self._resolve_track(track_index, track_scope)
        mixer_device = getattr(track, "mixer_device", None)
        if mixer_device is None or not hasattr(mixer_device, "sends"):
            raise ValueError("Track scope does not expose sends")

        sends = list(mixer_device.sends)
        send_index = int(send_index)
        if send_index < 0 or send_index >= len(sends):
            raise IndexError("Send index out of range")

        parameter = sends[send_index]
        send_name = ""
        if send_index < len(self._song.return_tracks):
            try:
                send_name = self._song.return_tracks[send_index].name
            except Exception:
                send_name = ""
        return track, parameter, send_name

    def _set_parameter_value(self, parameter, value, use_gesture=True):
        """Set a parameter value with optional begin/end gesture wrapping."""
        clamped = max(parameter.min, min(parameter.max, float(value)))
        gesture_started = False
        if use_gesture:
            try:
                parameter.begin_gesture()
                gesture_started = True
            except Exception:
                gesture_started = False
        try:
            parameter.value = clamped
        finally:
            if gesture_started:
                try:
                    parameter.end_gesture()
                except Exception:
                    pass
        return parameter.value

    def _routing_option_name(self, option):
        """Best-effort readable name for a routing option object."""
        if option is None:
            return ""
        for attribute in ["display_name", "name"]:
            try:
                value = getattr(option, attribute)
                if value is not None:
                    return str(value)
            except Exception:
                pass
        try:
            return str(option)
        except Exception:
            return ""

    def _serialize_routing_options(self, options):
        """Serialize a routing option list into readable names."""
        try:
            return [self._routing_option_name(option) for option in list(options)]
        except Exception:
            return []

    def _get_group_track_info(self, track):
        """Return grouping metadata for a track."""
        result = {
            "is_grouped": False,
            "group_track_index": None,
            "group_track_name": None
        }
        try:
            result["is_grouped"] = bool(track.is_grouped)
        except Exception:
            return result

        if not result["is_grouped"]:
            return result

        try:
            group_track = track.group_track
        except Exception:
            return result

        try:
            result["group_track_name"] = group_track.name
        except Exception:
            pass

        try:
            for candidate_index, candidate in enumerate(self._song.tracks):
                if candidate == group_track:
                    result["group_track_index"] = candidate_index
                    break
        except Exception:
            pass

        return result

    def _get_track_monitoring_summary(self, track):
        """Return current monitoring state and available states."""
        try:
            current_state = track.current_monitoring_state
        except Exception:
            current_state = None

        try:
            available_states = list(track.monitoring_states)
        except Exception:
            available_states = []

        state_values = []
        for state in available_states:
            try:
                state_values.append(int(state))
            except Exception:
                try:
                    state_values.append(str(state))
                except Exception:
                    pass

        return {
            "current": current_state,
            "available": state_values
        }

    def _get_track_routing_summary(self, track):
        """Return readable routing state and available routing options for a track."""
        summary = {
            "current_input_routing": "",
            "current_input_sub_routing": "",
            "current_output_routing": "",
            "current_output_sub_routing": "",
            "available_input_routings": [],
            "available_input_sub_routings": [],
            "available_output_routings": [],
            "available_output_sub_routings": []
        }

        try:
            summary["current_input_routing"] = self._routing_option_name(track.input_routing_type)
        except Exception:
            try:
                summary["current_input_routing"] = self._routing_option_name(track.current_input_routing)
            except Exception:
                pass

        try:
            summary["current_input_sub_routing"] = self._routing_option_name(track.input_routing_channel)
        except Exception:
            try:
                summary["current_input_sub_routing"] = self._routing_option_name(track.current_input_sub_routing)
            except Exception:
                pass

        try:
            summary["current_output_routing"] = self._routing_option_name(track.output_routing_type)
        except Exception:
            try:
                summary["current_output_routing"] = self._routing_option_name(track.current_output_routing)
            except Exception:
                pass

        try:
            summary["current_output_sub_routing"] = self._routing_option_name(track.output_routing_channel)
        except Exception:
            try:
                summary["current_output_sub_routing"] = self._routing_option_name(track.current_output_sub_routing)
            except Exception:
                pass

        for attr_name, result_key in [
            ("available_input_routing_types", "available_input_routings"),
            ("available_input_routing_channels", "available_input_sub_routings"),
            ("available_output_routing_types", "available_output_routings"),
            ("available_output_routing_channels", "available_output_sub_routings")
        ]:
            try:
                summary[result_key] = self._serialize_routing_options(getattr(track, attr_name))
            except Exception:
                summary[result_key] = []

        return summary

    def _match_routing_option(self, options, target_name):
        """Find a routing option by display name, case-insensitively."""
        normalized = (target_name or "").strip().lower()
        if not normalized:
            return None

        for option in list(options):
            if self._routing_option_name(option).strip().lower() == normalized:
                return option
        return None

    def _device_supports_input_routing(self, device):
        """Return whether a device exposes input-routing selectors."""
        for attribute in ["available_input_routing_types", "input_routing_type"]:
            if not hasattr(device, attribute):
                return False
        return True

    def _get_device_input_routing_summary(self, device):
        """Return readable input-routing state for a device that supports sidechain/source routing."""
        if not self._device_supports_input_routing(device):
            raise ValueError("Device does not expose input routing")

        summary = {
            "current_input_routing": "",
            "current_input_sub_routing": "",
            "available_input_routings": [],
            "available_input_sub_routings": []
        }

        try:
            summary["current_input_routing"] = self._routing_option_name(device.input_routing_type)
        except Exception:
            pass

        try:
            summary["current_input_sub_routing"] = self._routing_option_name(device.input_routing_channel)
        except Exception:
            pass

        try:
            summary["available_input_routings"] = self._serialize_routing_options(device.available_input_routing_types)
        except Exception:
            pass

        try:
            summary["available_input_sub_routings"] = self._serialize_routing_options(device.available_input_routing_channels)
        except Exception:
            pass

        return summary

    def _resolve_clip(self, track_index, clip_index, arrangement=False, arrangement_clip_index=0, track_scope="track"):
        """Resolve a session or arrangement clip from a track scope."""
        track = self._resolve_track(track_index, track_scope)

        if arrangement:
            clips = list(track.arrangement_clips)
            if arrangement_clip_index < 0 or arrangement_clip_index >= len(clips):
                raise IndexError("Arrangement clip index out of range")
            return track, clips[arrangement_clip_index]

        if clip_index < 0 or clip_index >= len(track.clip_slots):
            raise IndexError("Clip index out of range")

        clip_slot = track.clip_slots[clip_index]
        if not clip_slot.has_clip:
            raise ValueError("Clip slot is empty")

        return track, clip_slot.clip

    def _resolve_device(self, track_index, device_index, track_scope="track"):
        """Resolve a device from a track-like scope."""
        track = self._resolve_track(track_index, track_scope)
        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index out of range")
        return track, track.devices[device_index]

    def _resolve_container(self, track_index, track_scope="track", container_path=None):
        """Resolve a nested device container such as a track, chain, or drum pad chain."""
        if container_path is None:
            container_path = []

        current = self._resolve_track(track_index, track_scope)
        for segment in container_path:
            segment_type = segment.get("type", "")
            index = int(segment.get("index", 0))

            if segment_type == "device":
                devices = getattr(current, "devices", None)
                if devices is None:
                    raise ValueError("Current container has no devices")
                if index < 0 or index >= len(devices):
                    raise IndexError("Device index out of range in container_path")
                current = devices[index]
            elif segment_type == "chain":
                chains = getattr(current, "chains", None)
                if chains is None:
                    raise ValueError("Current object has no chains")
                if index < 0 or index >= len(chains):
                    raise IndexError("Chain index out of range in container_path")
                current = chains[index]
            elif segment_type == "return_chain":
                return_chains = getattr(current, "return_chains", None)
                if return_chains is None:
                    raise ValueError("Current object has no return chains")
                if index < 0 or index >= len(return_chains):
                    raise IndexError("Return chain index out of range in container_path")
                current = return_chains[index]
            elif segment_type == "drum_pad":
                drum_pads = getattr(current, "drum_pads", None)
                if drum_pads is None:
                    raise ValueError("Current object has no drum pads")
                if index < 0 or index >= len(drum_pads):
                    raise IndexError("Drum pad index out of range in container_path")
                current = drum_pads[index]
            elif segment_type == "drum_pad_chain":
                chains = getattr(current, "chains", None)
                if chains is None:
                    raise ValueError("Current drum pad has no chains")
                if index < 0 or index >= len(chains):
                    raise IndexError("Drum pad chain index out of range in container_path")
                current = chains[index]
            else:
                raise ValueError("Unknown container path segment type '{0}'".format(segment_type))

        return current

    def _resolve_device_in_container(self, track_index, track_scope="track", container_path=None, device_index=0):
        """Resolve a device inside a potentially nested container."""
        container = self._resolve_container(track_index, track_scope, container_path)
        devices = getattr(container, "devices", None)
        if devices is None:
            raise ValueError("Resolved container has no devices")
        if device_index < 0 or device_index >= len(devices):
            raise IndexError("Device index out of range")
        return container, devices[device_index]

    def _find_device_parameter(self, device, parameter_name):
        """Find a device parameter by name or original name."""
        needle = parameter_name.lower()
        for parameter in device.parameters:
            names = [parameter.name]
            try:
                names.append(parameter.original_name)
            except Exception:
                pass
            if any(name and name.lower() == needle for name in names):
                return parameter
        return None

    def _serialize_parameter(self, parameter):
        """Return richer telemetry for a device parameter."""
        parameter_info = {
            "name": parameter.name,
            "value": parameter.value,
            "min": parameter.min,
            "max": parameter.max,
            "is_quantized": parameter.is_quantized
        }
        try:
            parameter_info["original_name"] = parameter.original_name
        except Exception:
            pass
        try:
            parameter_info["display_value"] = parameter.display_value
        except Exception:
            pass
        try:
            parameter_info["automation_state"] = parameter.automation_state
        except Exception:
            pass
        try:
            parameter_info["default_value"] = parameter.default_value
        except Exception:
            pass
        try:
            if parameter.is_quantized:
                parameter_info["value_items"] = list(parameter.value_items)
        except Exception:
            pass
        return parameter_info

    def _serialize_device_topology(self, device, max_depth, current_depth, include_parameters, include_empty_drum_pads):
        """Serialize a device and any nested rack structure."""
        result = {
            "name": device.name,
            "class_name": device.class_name,
            "type": self._get_device_type(device)
        }

        try:
            result["can_have_chains"] = device.can_have_chains
        except Exception:
            pass
        try:
            result["can_have_drum_pads"] = device.can_have_drum_pads
        except Exception:
            pass

        if include_parameters:
            try:
                result["parameters"] = [self._serialize_parameter(parameter) for parameter in device.parameters]
            except Exception:
                pass

        if current_depth >= max_depth:
            return result

        try:
            chains = list(device.chains)
        except Exception:
            chains = []
        if chains:
            result["chains"] = [self._serialize_container_info(chain, max_depth, current_depth + 1,
                                                                 include_parameters, include_empty_drum_pads)
                                 for chain in chains]

        try:
            return_chains = list(device.return_chains)
        except Exception:
            return_chains = []
        if return_chains:
            result["return_chains"] = [self._serialize_container_info(chain, max_depth, current_depth + 1,
                                                                        include_parameters, include_empty_drum_pads)
                                        for chain in return_chains]

        try:
            drum_pads = list(device.drum_pads)
        except Exception:
            drum_pads = []
        if drum_pads:
            serialized_pads = []
            for pad in drum_pads:
                try:
                    pad_chains = list(pad.chains)
                except Exception:
                    pad_chains = []
                if not include_empty_drum_pads and not pad_chains:
                    continue
                pad_info = {
                    "name": pad.name
                }
                try:
                    pad_info["note"] = pad.note
                except Exception:
                    pass
                if pad_chains and current_depth < max_depth:
                    pad_info["chains"] = [self._serialize_container_info(chain, max_depth, current_depth + 1,
                                                                           include_parameters, include_empty_drum_pads)
                                           for chain in pad_chains]
                serialized_pads.append(pad_info)
            result["drum_pads"] = serialized_pads

        return result

    def _serialize_container_info(self, container, max_depth, current_depth=0,
                                  include_parameters=False, include_empty_drum_pads=False):
        """Serialize a device container such as a track or chain."""
        result = {}
        try:
            result["name"] = container.name
        except Exception:
            result["name"] = ""

        result["container_type"] = container.__class__.__name__

        try:
            result["volume"] = container.mixer_device.volume.value
            result["panning"] = container.mixer_device.panning.value
        except Exception:
            pass

        if current_depth > max_depth:
            return result

        try:
            devices = list(container.devices)
        except Exception:
            devices = []

        result["devices"] = []
        for index, device in enumerate(devices):
            device_info = self._serialize_device_topology(
                device,
                max_depth,
                current_depth,
                include_parameters,
                include_empty_drum_pads
            )
            device_info["index"] = index
            result["devices"].append(device_info)

        return result

    def _get_track_mixer_summary(self, track):
        """Return richer mixer-device telemetry for a track-like object."""
        mixer_device = getattr(track, "mixer_device", None)
        if mixer_device is None:
            return {}

        summary = {
            "volume": self._serialize_parameter(mixer_device.volume),
            "panning": self._serialize_parameter(mixer_device.panning),
            "sends": self._serialize_track_sends(track)
        }

        for attr_name, result_key in [
            ("track_activator", "track_activator"),
            ("crossfader", "crossfader"),
            ("cue_volume", "cue_volume")
        ]:
            try:
                summary[result_key] = self._serialize_parameter(getattr(mixer_device, attr_name))
            except Exception:
                pass

        for attr_name in ["crossfade_assign", "panning_mode", "left_split_stereo", "right_split_stereo"]:
            try:
                summary[attr_name] = getattr(mixer_device, attr_name)
            except Exception:
                pass

        return summary

    def _get_track_view_summary(self, track):
        """Return track-view selection/collapse telemetry."""
        try:
            view = track.view
        except Exception:
            return {}

        return {
            "is_collapsed": getattr(view, "is_collapsed", None),
            "device_insert_mode": getattr(view, "device_insert_mode", None),
            "selected_device": self._resolve_device_reference(getattr(view, "selected_device", None))
        }

    def _get_track_mixer(self, track_index, track_scope="track"):
        """Return mixer telemetry for a track-like object."""
        track = self._resolve_track(track_index, track_scope)
        return {
            "index": track_index,
            "name": getattr(track, "name", ""),
            "track_scope": track_scope,
            "mixer": self._get_track_mixer_summary(track)
        }

    def _get_track_view(self, track_index, track_scope="track"):
        """Return track-view telemetry for a track-like object."""
        track = self._resolve_track(track_index, track_scope)
        return {
            "index": track_index,
            "name": getattr(track, "name", ""),
            "track_scope": track_scope,
            "view": self._get_track_view_summary(track)
        }

    def _get_clip_slot_info(self, track_index, clip_index, track_scope="track"):
        """Return telemetry for a specific clip slot."""
        track = self._resolve_track(track_index, track_scope)
        clip_index = int(clip_index)
        clip_slots = list(getattr(track, "clip_slots", []))
        if clip_index < 0 or clip_index >= len(clip_slots):
            raise IndexError("Clip slot index out of range")
        return self._serialize_clip_slot(clip_slots[clip_index], track_index, clip_index)

    def _get_take_lanes(self, track_index, track_scope="track"):
        """Return take-lane telemetry for a track."""
        track = self._resolve_track(track_index, track_scope)
        take_lanes = []
        try:
            lanes = list(track.take_lanes)
        except Exception:
            lanes = []

        for lane_index, lane in enumerate(lanes):
            try:
                arrangement_clips = list(lane.arrangement_clips)
            except Exception:
                arrangement_clips = []
            take_lanes.append({
                "lane_index": lane_index,
                "name": getattr(lane, "name", ""),
                "color": getattr(lane, "color", None),
                "arrangement_clips_count": len(arrangement_clips),
                "arrangement_clip_names": [getattr(clip, "name", "") for clip in arrangement_clips]
            })

        return {
            "index": track_index,
            "name": getattr(track, "name", ""),
            "track_scope": track_scope,
            "take_lanes": take_lanes
        }

    def _get_track_info(self, track_index, track_scope="track"):
        """Get information about a track"""
        try:
            track = self._resolve_track(track_index, track_scope)
            if track_scope == "master" or track_index == -1:
                track_index = -1
            
            # Get clip slots
            clip_slots = []
            try:
                clip_slots_source = track.clip_slots
            except Exception:
                clip_slots_source = []
            for slot_index, slot in enumerate(clip_slots_source):
                clip_info = None
                if slot.has_clip:
                    clip = slot.clip
                    clip_info = {
                        "name": clip.name,
                        "length": clip.length,
                        "is_playing": clip.is_playing,
                        "is_recording": clip.is_recording,
                        "color": clip.color,
                        "has_envelopes": getattr(clip, "has_envelopes", False)
                    }

                clip_slots.append({
                    "index": slot_index,
                    "has_clip": slot.has_clip,
                    "clip": clip_info
                })
            
            # Get devices
            devices = []
            for device_index, device in enumerate(track.devices):
                devices.append({
                    "index": device_index,
                    "name": device.name,
                    "class_name": device.class_name,
                    "type": self._get_device_type(device)
                })
            
            # Some track scopes don't expose all track-style properties.
            try:
                arm = track.arm
            except Exception:
                arm = None
            try:
                mute = track.mute
            except Exception:
                mute = None
            try:
                solo = track.solo
            except Exception:
                solo = None
            try:
                has_audio_input = track.has_audio_input
            except Exception:
                has_audio_input = None
            try:
                has_midi_input = track.has_midi_input
            except Exception:
                has_midi_input = None
            try:
                color = track.color
            except Exception:
                color = None
            try:
                color_index = track.color_index
            except Exception:
                color_index = None

            group_info = self._get_group_track_info(track)
            routing_info = self._get_track_routing_summary(track)
            monitoring_info = self._get_track_monitoring_summary(track)
            mixer_info = self._get_track_mixer_summary(track)
            track_view_info = self._get_track_view_summary(track)

            # Group tracks are foldable in Ableton's API
            try:
                is_group_track = track.is_foldable
            except Exception:
                is_group_track = False

            extra_track_state = {}
            for attr_name in [
                "can_be_armed", "can_be_frozen", "can_show_chains", "is_frozen",
                "is_showing_chains", "is_visible", "back_to_arranger", "playing_slot_index",
                "fired_slot_index", "muted_via_solo", "implicit_arm", "performance_impact",
                "fold_state", "is_part_of_selection"
            ]:
                try:
                    extra_track_state[attr_name] = getattr(track, attr_name)
                except Exception:
                    extra_track_state[attr_name] = None

            # Arrangement clips exist on the timeline (Arrangement View)
            try:
                arrangement_clips = list(track.arrangement_clips)
                arrangement_clip_names = [clip.name for clip in arrangement_clips]
                arrangement_clip_colors = [clip.color for clip in arrangement_clips]
                arrangement_clips_count = len(arrangement_clips)
            except Exception:
                arrangement_clips_count = 0
                arrangement_clip_names = []
                arrangement_clip_colors = []

            result = {
                "index": track_index,
                "name": track.name,
                "track_scope": track_scope,
                "color": color,
                "color_index": color_index,
                "is_audio_track": has_audio_input,
                "is_midi_track": has_midi_input,
                "mute": mute,
                "solo": solo,
                "arm": arm,
                "is_group_track": is_group_track,
                "is_grouped": group_info["is_grouped"],
                "group_track_index": group_info["group_track_index"],
                "group_track_name": group_info["group_track_name"],
                "volume": track.mixer_device.volume.value,
                "panning": track.mixer_device.panning.value,
                "monitoring": monitoring_info,
                "routing": routing_info,
                "mixer": mixer_info,
                "view": track_view_info,
                "sends": self._serialize_track_sends(track),
                "clip_slots": clip_slots,
                "devices": devices,
                "arrangement_clips_count": arrangement_clips_count,
                "arrangement_clip_names": arrangement_clip_names,
                "arrangement_clip_colors": arrangement_clip_colors,
                "take_lanes": self._get_take_lanes(track_index, track_scope).get("take_lanes", [])
            }
            result.update(extra_track_state)
            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise

    def _get_track_sends(self, track_index, track_scope="track"):
        """Return send values for a track or group track."""
        try:
            track = self._resolve_track(track_index, track_scope)
            return {
                "index": track_index,
                "name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "sends": self._serialize_track_sends(track)
            }
        except Exception as e:
            self.log_message("Error getting track sends: " + str(e))
            raise

    def _get_track_routing(self, track_index, track_scope="track"):
        """Return routing and monitoring telemetry for a track."""
        try:
            track = self._resolve_track(track_index, track_scope)
            return {
                "index": track_index,
                "name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "routing": self._get_track_routing_summary(track),
                "monitoring": self._get_track_monitoring_summary(track)
            }
        except Exception as e:
            self.log_message("Error getting track routing: " + str(e))
            raise

    def _get_device_topology(self, track_index, track_scope="track", container_path=None, max_depth=4,
                             include_parameters=False, include_empty_drum_pads=False):
        """Return recursive topology for devices, chains, and drum pads."""
        try:
            container = self._resolve_container(track_index, track_scope, container_path)
            result = self._serialize_container_info(
                container,
                int(max_depth),
                0,
                include_parameters,
                include_empty_drum_pads
            )
            result["track_scope"] = track_scope
            result["track_index"] = track_index
            result["container_path"] = container_path or []
            return result
        except Exception as e:
            self.log_message("Error getting device topology: " + str(e))
            raise

    def _debug_object_methods(self, object_type, track_index, arrangement_clip_index, clip_index=0,
                              device_index=0, parameter_name="", arrangement=False, track_scope="track",
                              container_path=None, parameter_source="device", send_index=0):
        """Return a filtered method/property list for a Live API object."""
        try:
            if object_type == "song":
                obj = self._song
            elif object_type == "song_view":
                obj = self._song.view
            elif object_type == "application_view":
                obj = self.application().view
            elif object_type == "scene":
                obj = self._resolve_scene(track_index)
            elif object_type == "cue_point":
                cue_points = list(getattr(self._song, "cue_points", []))
                if track_index < 0 or track_index >= len(cue_points):
                    raise IndexError("Cue point index out of range")
                obj = cue_points[track_index]
            elif object_type == "master_track":
                obj = self._song.master_track
            elif object_type == "return_track":
                obj = self._resolve_track(track_index, "return")
            elif object_type == "track":
                obj = self._resolve_track(track_index, track_scope)
            elif object_type == "track_view":
                obj = self._resolve_track(track_index, track_scope).view
            elif object_type == "mixer_device":
                obj = self._resolve_track(track_index, track_scope).mixer_device
            elif object_type == "clip_slot":
                track = self._resolve_track(track_index, track_scope)
                obj = list(track.clip_slots)[clip_index]
            elif object_type == "take_lane":
                track = self._resolve_track(track_index, track_scope)
                lanes = list(getattr(track, "take_lanes", []))
                if arrangement_clip_index < 0 or arrangement_clip_index >= len(lanes):
                    raise IndexError("Take lane index out of range")
                obj = lanes[arrangement_clip_index]
            elif object_type == "container":
                obj = self._resolve_container(track_index, track_scope, container_path)
            elif object_type == "chain":
                obj = self._resolve_container(track_index, track_scope, container_path)
            elif object_type == "drum_pad":
                obj = self._resolve_container(track_index, track_scope, container_path)
            elif object_type == "session_clip":
                obj = self._resolve_clip(track_index, clip_index, False, 0, track_scope)[1]
            elif object_type == "arrangement_clip":
                if track_index < 0 or track_index >= len(self._song.tracks):
                    raise IndexError("Track index out of range")
                clips = list(self._song.tracks[track_index].arrangement_clips)
                if arrangement_clip_index < 0 or arrangement_clip_index >= len(clips):
                    raise IndexError("Arrangement clip index out of range")
                obj = clips[arrangement_clip_index]
            elif object_type == "device":
                _, obj = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
            elif object_type == "device_parameter":
                _, device = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
                parameter = self._find_device_parameter(device, parameter_name)
                if parameter is None:
                    raise ValueError("Parameter '{0}' not found".format(parameter_name))
                obj = parameter
            elif object_type == "clip_automation_envelope":
                _, _, _, _, obj, _ = self._get_clip_automation_envelope(
                    track_index,
                    clip_index,
                    arrangement,
                    arrangement_clip_index,
                    device_index,
                    parameter_name,
                    parameter_source,
                    send_index,
                    track_scope,
                    False,
                    container_path
                )
                if obj is None:
                    raise ValueError("No automation envelope for parameter '{0}'".format(parameter_name))
            else:
                raise ValueError("Unknown object_type")

            names = []
            for name in dir(obj):
                if name.startswith("_"):
                    continue
                names.append(name)

            return {
                "object_type": object_type,
                "methods": sorted(names)
            }
        except Exception as e:
            self.log_message("Error debugging object methods: " + str(e))
            raise

    def _get_clip_info(self, track_index, clip_index, arrangement=False, arrangement_clip_index=0, track_scope="track"):
        """Get information about a session or arrangement clip."""
        try:
            _, clip = self._resolve_clip(
                track_index,
                clip_index,
                arrangement,
                arrangement_clip_index,
                track_scope
            )

            try:
                automation_envelopes = list(clip.automation_envelopes)
                automation_envelope_count = len(automation_envelopes)
            except Exception:
                automation_envelope_count = 0

            return {
                "name": clip.name,
                "track_scope": track_scope,
                "arrangement": arrangement,
                "length": clip.length,
                "color": clip.color,
                "is_audio_clip": clip.is_audio_clip,
                "is_midi_clip": clip.is_midi_clip,
                "is_session_clip": clip.is_session_clip,
                "is_arrangement_clip": clip.is_arrangement_clip,
                "is_playing": clip.is_playing,
                "is_recording": clip.is_recording,
                "looping": clip.looping,
                "position": clip.position,
                "loop_start": clip.loop_start,
                "loop_end": clip.loop_end,
                "start_marker": clip.start_marker,
                "end_marker": clip.end_marker,
                "has_envelopes": clip.has_envelopes,
                "automation_envelope_count": automation_envelope_count
            }
        except Exception as e:
            self.log_message("Error getting clip info: " + str(e))
            raise

    def _resolve_clip_automation_parameter(self, track_index, device_index, parameter_name,
                                           track_scope="track", container_path=None,
                                           parameter_source="device", send_index=0):
        """Resolve a clip-automatable parameter from a device or track mixer."""
        if parameter_source == "device":
            _, device = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
            parameter = self._find_device_parameter(device, parameter_name)
            if parameter is None:
                raise ValueError("Parameter '{0}' not found".format(parameter_name))
            return device, parameter, {
                "parameter_source": "device",
                "device_name": device.name
            }

        track = self._resolve_track(track_index, track_scope)
        mixer_device = getattr(track, "mixer_device", None)
        if mixer_device is None:
            raise ValueError("Track scope does not expose a mixer device")

        if parameter_source == "mixer_volume":
            parameter = mixer_device.volume
            return _NamedParameterOwner("Mixer"), parameter, {
                "parameter_source": "mixer_volume",
                "device_name": "Mixer"
            }

        if parameter_source == "mixer_panning":
            parameter = mixer_device.panning
            return _NamedParameterOwner("Mixer"), parameter, {
                "parameter_source": "mixer_panning",
                "device_name": "Mixer"
            }

        if parameter_source == "mixer_send":
            sends = list(mixer_device.sends)
            send_index = int(send_index)
            if send_index < 0 or send_index >= len(sends):
                raise IndexError("Send index out of range")
            parameter = sends[send_index]
            send_name = ""
            if send_index < len(self._song.return_tracks):
                try:
                    send_name = self._song.return_tracks[send_index].name
                except Exception:
                    send_name = ""
            owner_name = "Mixer Send"
            if send_name:
                owner_name = "Mixer Send {0}".format(send_name)
            return _NamedParameterOwner(owner_name), parameter, {
                "parameter_source": "mixer_send",
                "device_name": owner_name,
                "send_index": send_index,
                "send_name": send_name
            }

        raise ValueError("Unsupported parameter_source '{0}'".format(parameter_source))

    def _get_clip_automation_envelope(self, track_index, clip_index, arrangement, arrangement_clip_index,
                                      device_index, parameter_name, parameter_source="device", send_index=0,
                                      track_scope="track", create_if_missing=False, container_path=None):
        """Resolve a clip automation envelope for a device or mixer parameter."""
        track, clip = self._resolve_clip(
            track_index,
            clip_index,
            arrangement,
            arrangement_clip_index,
            track_scope
        )
        owner, parameter, parameter_info = self._resolve_clip_automation_parameter(
            track_index,
            device_index,
            parameter_name,
            track_scope,
            container_path,
            parameter_source,
            send_index
        )

        envelope = clip.automation_envelope(parameter)
        if envelope is None and create_if_missing:
            clip.create_automation_envelope(parameter)
            envelope = clip.automation_envelope(parameter)

        return track, clip, owner, parameter, envelope, parameter_info

    def _clear_clip_automation(self, track_index, clip_index, arrangement=False, arrangement_clip_index=0,
                               device_index=0, parameter_name="", parameter_source="device", send_index=0,
                               track_scope="track", container_path=None):
        """Clear clip automation for a specific device parameter."""
        try:
            track, clip, owner, parameter, _, parameter_info = self._get_clip_automation_envelope(
                track_index,
                clip_index,
                arrangement,
                arrangement_clip_index,
                device_index,
                parameter_name,
                parameter_source,
                send_index,
                track_scope,
                False,
                container_path
            )
            clip.clear_envelope(parameter)

            return {
                "track_name": track.name,
                "track_scope": track_scope,
                "clip_name": clip.name,
                "arrangement": arrangement,
                "device_name": owner.name,
                "parameter_name": parameter.name,
                "parameter_source": parameter_info.get("parameter_source", parameter_source),
                "send_index": parameter_info.get("send_index"),
                "send_name": parameter_info.get("send_name"),
                "cleared": True
            }
        except Exception as e:
            self.log_message("Error clearing clip automation: " + str(e))
            raise

    def _set_clip_automation_steps(self, track_index, clip_index, arrangement=False, arrangement_clip_index=0,
                                   device_index=0, parameter_name="", parameter_source="device", send_index=0,
                                   steps=None, clear_existing=True, track_scope="track", container_path=None):
        """Create or update a clip automation envelope with step segments."""
        try:
            if steps is None:
                steps = []

            track, clip, owner, parameter, envelope, parameter_info = self._get_clip_automation_envelope(
                track_index,
                clip_index,
                arrangement,
                arrangement_clip_index,
                device_index,
                parameter_name,
                parameter_source,
                send_index,
                track_scope,
                True,
                container_path
            )

            if clear_existing:
                clip.clear_envelope(parameter)
                envelope = clip.automation_envelope(parameter)
                if envelope is None:
                    clip.create_automation_envelope(parameter)
                    envelope = clip.automation_envelope(parameter)

            inserted_steps = []
            for step in steps:
                start_time = float(step.get("start_time", 0.0))
                duration = float(step.get("duration", 0.25))
                raw_value = float(step.get("value", parameter.value))
                clamped = max(parameter.min, min(parameter.max, raw_value))
                envelope.insert_step(start_time, duration, clamped)
                inserted_steps.append({
                    "start_time": start_time,
                    "duration": duration,
                    "value": clamped
                })

            return {
                "track_name": track.name,
                "track_scope": track_scope,
                "clip_name": clip.name,
                "arrangement": arrangement,
                "device_name": owner.name,
                "parameter_name": parameter.name,
                "parameter_source": parameter_info.get("parameter_source", parameter_source),
                "send_index": parameter_info.get("send_index"),
                "send_name": parameter_info.get("send_name"),
                "step_count": len(inserted_steps),
                "clear_existing": clear_existing,
                "steps": inserted_steps
            }
        except Exception as e:
            self.log_message("Error setting clip automation: " + str(e))
            raise

    def _sample_clip_automation(self, track_index, clip_index, arrangement=False, arrangement_clip_index=0,
                                device_index=0, parameter_name="", parameter_source="device", send_index=0,
                                sample_times=None, track_scope="track", container_path=None):
        """Sample automation envelope values at specific times."""
        try:
            if sample_times is None:
                sample_times = []

            track, clip, owner, parameter, envelope, parameter_info = self._get_clip_automation_envelope(
                track_index,
                clip_index,
                arrangement,
                arrangement_clip_index,
                device_index,
                parameter_name,
                parameter_source,
                send_index,
                track_scope,
                False,
                container_path
            )
            if envelope is None:
                raise ValueError("No automation envelope for parameter '{0}'".format(parameter_name))

            samples = []
            for time_value in sample_times:
                current_time = float(time_value)
                samples.append({
                    "time": current_time,
                    "value": envelope.value_at_time(current_time)
                })

            return {
                "track_name": track.name,
                "track_scope": track_scope,
                "clip_name": clip.name,
                "arrangement": arrangement,
                "device_name": owner.name,
                "parameter_name": parameter.name,
                "parameter_source": parameter_info.get("parameter_source", parameter_source),
                "send_index": parameter_info.get("send_index"),
                "send_name": parameter_info.get("send_name"),
                "samples": samples
            }
        except Exception as e:
            self.log_message("Error sampling clip automation: " + str(e))
            raise

    def _get_clip_automation_events(self, track_index, clip_index, arrangement=False, arrangement_clip_index=0,
                                    device_index=0, parameter_name="", parameter_source="device", send_index=0,
                                    start_time=0.0, end_time=0.0, track_scope="track", container_path=None):
        """Read automation events in a time range."""
        try:
            track, clip, owner, parameter, envelope, parameter_info = self._get_clip_automation_envelope(
                track_index,
                clip_index,
                arrangement,
                arrangement_clip_index,
                device_index,
                parameter_name,
                parameter_source,
                send_index,
                track_scope,
                False,
                container_path
            )
            if envelope is None:
                raise ValueError("No automation envelope for parameter '{0}'".format(parameter_name))

            events = []
            for event in envelope.events_in_range(float(start_time), float(end_time)):
                event_info = {}
                for name in dir(event):
                    if name.startswith("_"):
                        continue
                    try:
                        value = getattr(event, name)
                    except Exception:
                        continue
                    if callable(value):
                        continue
                    if isinstance(value, (bool, int, float, str)):
                        event_info[name] = value
                events.append(event_info)

            return {
                "track_name": track.name,
                "track_scope": track_scope,
                "clip_name": clip.name,
                "arrangement": arrangement,
                "device_name": owner.name,
                "parameter_name": parameter.name,
                "parameter_source": parameter_info.get("parameter_source", parameter_source),
                "send_index": parameter_info.get("send_index"),
                "send_name": parameter_info.get("send_name"),
                "start_time": float(start_time),
                "end_time": float(end_time),
                "events": events
            }
        except Exception as e:
            self.log_message("Error getting clip automation events: " + str(e))
            raise

    def _delete_clip_automation_events(self, track_index, clip_index, arrangement=False, arrangement_clip_index=0,
                                       device_index=0, parameter_name="", parameter_source="device", send_index=0,
                                       start_time=0.0, end_time=0.0, track_scope="track", container_path=None):
        """Delete automation events in a time range."""
        try:
            track, clip, owner, parameter, envelope, parameter_info = self._get_clip_automation_envelope(
                track_index,
                clip_index,
                arrangement,
                arrangement_clip_index,
                device_index,
                parameter_name,
                parameter_source,
                send_index,
                track_scope,
                False,
                container_path
            )
            if envelope is None:
                raise ValueError("No automation envelope for parameter '{0}'".format(parameter_name))

            envelope.delete_events_in_range(float(start_time), float(end_time))

            return {
                "track_name": track.name,
                "track_scope": track_scope,
                "clip_name": clip.name,
                "arrangement": arrangement,
                "device_name": owner.name,
                "parameter_name": parameter.name,
                "parameter_source": parameter_info.get("parameter_source", parameter_source),
                "send_index": parameter_info.get("send_index"),
                "send_name": parameter_info.get("send_name"),
                "start_time": float(start_time),
                "end_time": float(end_time),
                "deleted": True
            }
        except Exception as e:
            self.log_message("Error deleting clip automation events: " + str(e))
            raise

    def _get_clip_notes(self, track_index, clip_index, arrangement=False, arrangement_clip_index=0):
        """Read MIDI notes from a session or arrangement clip."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if arrangement:
                clips = list(track.arrangement_clips)
                if arrangement_clip_index < 0 or arrangement_clip_index >= len(clips):
                    raise IndexError("Arrangement clip index out of range")
                clip = clips[arrangement_clip_index]
            else:
                if clip_index < 0 or clip_index >= len(track.clip_slots):
                    raise IndexError("Clip index out of range")
                clip_slot = track.clip_slots[clip_index]
                if not clip_slot.has_clip:
                    raise ValueError("Clip slot is empty")
                clip = clip_slot.clip

            raw_notes = clip.get_all_notes_extended()
            notes = []
            for note in raw_notes:
                note_data = {
                    "pitch": note.pitch,
                    "start_time": note.start_time,
                    "duration": note.duration,
                    "velocity": note.velocity,
                    "mute": note.mute
                }
                notes.append(note_data)

            return {
                "note_count": len(notes),
                "notes": notes
            }
        except Exception as e:
            self.log_message("Error getting clip notes: " + str(e))
            raise

    def _get_device_parameters(self, track_index, device_index, track_scope="track", container_path=None):
        """Read device parameter metadata and current values."""
        try:
            container, device = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
            parameters = []
            for parameter in device.parameters:
                parameters.append(self._serialize_parameter(parameter))

            return {
                "track_name": getattr(container, "name", ""),
                "track_scope": track_scope,
                "container_path": container_path or [],
                "device_name": device.name,
                "device_index": device_index,
                "parameters": parameters
            }
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _get_device_input_routing(self, track_index, device_index, track_scope="track", container_path=None):
        """Read device input-routing state for devices that expose sidechain/source selectors."""
        try:
            container, device = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
            return {
                "track_name": getattr(container, "name", ""),
                "track_scope": track_scope,
                "container_path": container_path or [],
                "device_name": device.name,
                "device_index": device_index,
                "routing": self._get_device_input_routing_summary(device)
            }
        except Exception as e:
            self.log_message("Error getting device input routing: " + str(e))
            raise

    def _set_device_input_routing(self, track_index, device_index, routing_name, sub_routing_name="",
                                  track_scope="track", container_path=None):
        """Set a device's input-routing type/channel by readable names."""
        try:
            container, device = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
            if not self._device_supports_input_routing(device):
                raise ValueError("Device does not expose input routing")

            if routing_name:
                routing_option = self._match_routing_option(device.available_input_routing_types, routing_name)
                if routing_option is None:
                    raise ValueError("Device input routing '{0}' not found".format(routing_name))
                device.input_routing_type = routing_option

            if sub_routing_name:
                channel_option = self._match_routing_option(device.available_input_routing_channels, sub_routing_name)
                if channel_option is None:
                    raise ValueError("Device input sub-routing '{0}' not found".format(sub_routing_name))
                device.input_routing_channel = channel_option

            return {
                "track_name": getattr(container, "name", ""),
                "track_scope": track_scope,
                "container_path": container_path or [],
                "device_name": device.name,
                "device_index": device_index,
                "routing": self._get_device_input_routing_summary(device)
            }
        except Exception as e:
            self.log_message("Error setting device input routing: " + str(e))
            raise

    def _set_device_parameter(self, track_index, device_index, parameter_name, value, track_scope="track",
                              container_path=None):
        """Set a device parameter by displayed name or original name."""
        try:
            container, device = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
            target = self._find_device_parameter(device, parameter_name)
            if target is None:
                raise ValueError("Parameter '{0}' not found".format(parameter_name))

            clamped = max(target.min, min(target.max, float(value)))
            target.value = clamped

            return {
                "track_name": getattr(container, "name", ""),
                "track_scope": track_scope,
                "container_path": container_path or [],
                "device_name": device.name,
                "parameter_name": target.name,
                "value": target.value,
                "display_value": getattr(target, "display_value", None),
                "automation_state": getattr(target, "automation_state", None)
            }
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

    def _set_device_parameters(self, track_index, device_index, parameter_values, track_scope="track",
                               container_path=None):
        """Set multiple device parameters in one request."""
        try:
            container, device = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
            updated = []
            for parameter_name, value in parameter_values.items():
                target = self._find_device_parameter(device, parameter_name)
                if target is None:
                    raise ValueError("Parameter '{0}' not found".format(parameter_name))

                clamped = max(target.min, min(target.max, float(value)))
                target.value = clamped
                updated.append({
                    "parameter_name": target.name,
                    "value": target.value,
                    "display_value": getattr(target, "display_value", None),
                    "automation_state": getattr(target, "automation_state", None)
                })

            return {
                "track_name": getattr(container, "name", ""),
                "track_scope": track_scope,
                "container_path": container_path or [],
                "device_name": device.name,
                "device_index": device_index,
                "updated": updated
            }
        except Exception as e:
            self.log_message("Error setting device parameters: " + str(e))
            raise

    def _find_last_device_index_by_name(self, track, device_name):
        """Find the last device index matching a name."""
        for index in range(len(track.devices) - 1, -1, -1):
            if track.devices[index].name == device_name:
                return index
        return -1

    def _set_named_parameter(self, device, parameter_name, value):
        """Set a device parameter on a device object by name."""
        needle = parameter_name.lower()
        target = None
        for parameter in device.parameters:
            names = [parameter.name]
            try:
                names.append(parameter.original_name)
            except Exception:
                pass
            if any(name and name.lower() == needle for name in names):
                target = parameter
                break

        if target is None:
            raise ValueError("Parameter '{0}' not found".format(parameter_name))

        clamped = max(target.min, min(target.max, float(value)))
        target.value = clamped
        return target.value

    def _freq_to_eq8_norm(self, frequency_hz):
        """Convert Hz to EQ Eight's normalized frequency parameter."""
        try:
            frequency_hz = max(20.0, min(20000.0, float(frequency_hz)))
            return math.log(frequency_hz / 20.0, 1000.0)
        except Exception:
            return 0.0

    def _apply_cleanup_eq8(self, track_index, low_cut_hz, high_cut_hz, load_if_missing=True):
        """Add/configure an EQ Eight as a cleanup-only high-pass and low-pass stage."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            device_index = self._find_last_device_index_by_name(track, "EQ Eight")
            loaded = False

            if device_index < 0:
                if not load_if_missing:
                    return {
                        "track_name": track.name,
                        "status": "missing_eq8"
                    }

                app = self.application()
                item = self._find_browser_item_by_uri(app.browser, "query:AudioFx#EQ%20Eight")
                if not item:
                    raise ValueError("EQ Eight browser item not found")

                self._song.view.selected_track = track
                app.browser.load_item(item)
                loaded = True
                device_index = self._find_last_device_index_by_name(track, "EQ Eight")
                if device_index < 0:
                    raise RuntimeError("EQ Eight did not appear after loading")

            device = track.devices[device_index]
            low_norm = self._freq_to_eq8_norm(low_cut_hz)
            high_norm = self._freq_to_eq8_norm(high_cut_hz)

            self._set_named_parameter(device, "Device On", 1)
            self._set_named_parameter(device, "Output Gain", 0)
            self._set_named_parameter(device, "1 Filter On A", 1)
            self._set_named_parameter(device, "1 Filter Type A", 0)
            self._set_named_parameter(device, "1 Frequency A", low_norm)
            self._set_named_parameter(device, "8 Filter On A", 1)
            self._set_named_parameter(device, "8 Filter Type A", 7)
            self._set_named_parameter(device, "8 Frequency A", high_norm)

            for band in range(2, 8):
                self._set_named_parameter(device, "{0} Filter On A".format(band), 0)
            for band in range(1, 9):
                self._set_named_parameter(device, "{0} Filter On B".format(band), 0)

            return {
                "track_name": track.name,
                "device_index": device_index,
                "loaded": loaded,
                "low_cut_hz": float(low_cut_hz),
                "high_cut_hz": float(high_cut_hz)
            }
        except Exception as e:
            self.log_message("Error applying cleanup EQ8: " + str(e))
            raise
    
    def _create_midi_track(self, index):
        """Create a new MIDI track at the specified index"""
        try:
            # Create the track
            self._song.create_midi_track(index)
            
            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            
            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating MIDI track: " + str(e))
            raise

    def _create_audio_track(self, index):
        """Create a new audio track at the specified index."""
        try:
            self._song.create_audio_track(index)

            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]

            return {
                "index": new_track_index,
                "name": new_track.name
            }
        except Exception as e:
            self.log_message("Error creating audio track: " + str(e))
            raise

    def _create_return_track(self):
        """Create a new return track at the end of the returns list."""
        try:
            previous_count = len(self._song.return_tracks)
            self._song.create_return_track()

            new_track_index = len(self._song.return_tracks) - 1
            if new_track_index < previous_count:
                raise RuntimeError("Return track creation did not append a new return track")

            new_track = self._song.return_tracks[new_track_index]

            return {
                "index": new_track_index,
                "name": new_track.name
            }
        except Exception as e:
            self.log_message("Error creating return track: " + str(e))
            raise

    def _delete_return_track(self, track_index):
        """Delete a return track by index."""
        try:
            track_index = int(track_index)
            if track_index < 0 or track_index >= len(self._song.return_tracks):
                raise IndexError("Return track index out of range")
            deleted_name = getattr(self._song.return_tracks[track_index], "name", "")
            self._song.delete_return_track(track_index)
            return {
                "deleted_index": track_index,
                "deleted_name": deleted_name,
                "remaining_return_track_count": len(self._song.return_tracks)
            }
        except Exception as e:
            self.log_message("Error deleting return track: " + str(e))
            raise

    def _create_scene(self, index):
        """Create a new scene at the specified index."""
        try:
            self._song.create_scene(index)
            new_scene_index = len(self._song.scenes) - 1 if int(index) == -1 else int(index)
            scene = self._resolve_scene(new_scene_index)
            return self._describe_scene(scene, new_scene_index)
        except Exception as e:
            self.log_message("Error creating scene: " + str(e))
            raise

    def _duplicate_scene(self, scene_index):
        """Duplicate a scene at the specified index."""
        try:
            scene_index = int(scene_index)
            source_name = self._resolve_scene(scene_index).name
            self._song.duplicate_scene(scene_index)
            return {
                "source_scene_index": scene_index,
                "source_scene_name": source_name,
                "duplicated_scene_index": scene_index + 1,
                "scene": self._describe_scene(self._resolve_scene(scene_index + 1), scene_index + 1)
            }
        except Exception as e:
            self.log_message("Error duplicating scene: " + str(e))
            raise

    def _delete_scene(self, scene_index):
        """Delete a scene by index."""
        try:
            scene_index = int(scene_index)
            scene = self._resolve_scene(scene_index)
            deleted_name = getattr(scene, "name", "")
            self._song.delete_scene(scene_index)
            return {
                "deleted_scene_index": scene_index,
                "deleted_scene_name": deleted_name,
                "remaining_scene_count": len(self._song.scenes)
            }
        except Exception as e:
            self.log_message("Error deleting scene: " + str(e))
            raise

    def _set_scene_name(self, scene_index, name):
        """Rename a scene."""
        try:
            scene = self._resolve_scene(scene_index)
            scene.name = name
            return self._get_scene_info(scene_index)
        except Exception as e:
            self.log_message("Error setting scene name: " + str(e))
            raise

    def _set_scene_color(self, scene_index, color):
        """Set a scene color."""
        try:
            scene = self._resolve_scene(scene_index)
            scene.color = int(color)
            return self._get_scene_info(scene_index)
        except Exception as e:
            self.log_message("Error setting scene color: " + str(e))
            raise

    def _set_scene_tempo(self, scene_index, value):
        """Set a scene tempo."""
        try:
            scene = self._resolve_scene(scene_index)
            scene.tempo = float(value)
            return self._get_scene_info(scene_index)
        except Exception as e:
            self.log_message("Error setting scene tempo: " + str(e))
            raise

    def _set_scene_tempo_enabled(self, scene_index, enabled):
        """Enable or disable scene tempo launch override."""
        try:
            scene = self._resolve_scene(scene_index)
            scene.tempo_enabled = bool(enabled)
            return self._get_scene_info(scene_index)
        except Exception as e:
            self.log_message("Error setting scene tempo enabled: " + str(e))
            raise

    def _set_scene_time_signature(self, scene_index, numerator, denominator):
        """Set a scene time signature."""
        try:
            scene = self._resolve_scene(scene_index)
            scene.time_signature_numerator = int(numerator)
            scene.time_signature_denominator = int(denominator)
            return self._get_scene_info(scene_index)
        except Exception as e:
            self.log_message("Error setting scene time signature: " + str(e))
            raise

    def _set_scene_time_signature_enabled(self, scene_index, enabled):
        """Enable or disable scene time-signature launch override."""
        try:
            scene = self._resolve_scene(scene_index)
            scene.time_signature_enabled = bool(enabled)
            return self._get_scene_info(scene_index)
        except Exception as e:
            self.log_message("Error setting scene time signature enabled: " + str(e))
            raise

    def _set_scene_fire_button_state(self, scene_index, enabled):
        """Set a scene fire-button state."""
        try:
            scene = self._resolve_scene(scene_index)
            scene.set_fire_button_state(bool(enabled))
            return self._get_scene_info(scene_index)
        except Exception as e:
            self.log_message("Error setting scene fire button state: " + str(e))
            raise

    def _duplicate_track(self, track_index):
        """Duplicate a track at the specified index."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            source_name = self._song.tracks[track_index].name
            self._song.duplicate_track(track_index)

            return {
                "source_index": track_index,
                "source_name": source_name,
                "duplicated_index": track_index + 1
            }
        except Exception as e:
            self.log_message("Error duplicating track: " + str(e))
            raise
    
    
    def _set_track_name(self, track_index, name, track_scope="track"):
        """Set the name of a track"""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.name = name
            
            result = {
                "track_scope": track_scope,
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise

    def _set_track_color(self, track_index, color, track_scope="track"):
        """Set the color of a track using Ableton's internal color index."""
        try:
            track = self._resolve_track(track_index, track_scope)
            color = max(0, int(color))
            track.color = color

            result = {
                "track_scope": track_scope,
                "color": track.color
            }
            return result
        except Exception as e:
            self.log_message("Error setting track color: " + str(e))
            raise

    def _set_track_volume(self, track_index, value):
        """Set a track volume value on the mixer device."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            parameter = track.mixer_device.volume
            clamped = max(parameter.min, min(parameter.max, float(value)))
            parameter.value = clamped

            return {
                "track_name": track.name,
                "volume": parameter.value
            }
        except Exception as e:
            self.log_message("Error setting track volume: " + str(e))
            raise

    def _set_track_panning(self, track_index, value, track_scope="track"):
        """Set track panning on a track-like object."""
        try:
            track = self._resolve_track(track_index, track_scope)
            parameter = track.mixer_device.panning
            self._set_parameter_value(parameter, value)
            return {
                "track_name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "panning": parameter.value,
                "display_value": getattr(parameter, "display_value", None)
            }
        except Exception as e:
            self.log_message("Error setting track panning: " + str(e))
            raise

    def _set_track_mute(self, track_index, enabled, track_scope="track"):
        """Mute or unmute a track-like object."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.mute = bool(enabled)
            return self._get_track_info(track_index, track_scope)
        except Exception as e:
            self.log_message("Error setting track mute: " + str(e))
            raise

    def _set_track_solo(self, track_index, enabled, track_scope="track"):
        """Solo or unsolo a track-like object."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.solo = bool(enabled)
            return self._get_track_info(track_index, track_scope)
        except Exception as e:
            self.log_message("Error setting track solo: " + str(e))
            raise

    def _set_track_arm(self, track_index, enabled, track_scope="track"):
        """Arm or disarm a track-like object when supported."""
        try:
            track = self._resolve_track(track_index, track_scope)
            if not getattr(track, "can_be_armed", False):
                raise ValueError("Track cannot be armed")
            track.arm = bool(enabled)
            return self._get_track_info(track_index, track_scope)
        except Exception as e:
            self.log_message("Error setting track arm: " + str(e))
            raise

    def _set_track_activator(self, track_index, enabled, track_scope="track"):
        """Enable or disable the mixer track activator."""
        try:
            track = self._resolve_track(track_index, track_scope)
            parameter = track.mixer_device.track_activator
            self._set_parameter_value(parameter, 1.0 if enabled else 0.0)
            return self._get_track_mixer(track_index, track_scope)
        except Exception as e:
            self.log_message("Error setting track activator: " + str(e))
            raise

    def _coerce_crossfade_assign(self, value):
        """Map readable crossfade-assign names to integer values."""
        if isinstance(value, string_types):
            mapping = {
                "a": 0,
                "left": 0,
                "none": 1,
                "off": 1,
                "b": 2,
                "right": 2
            }
            lowered = value.strip().lower()
            if lowered not in mapping:
                raise ValueError("Unknown crossfade assign '{0}'".format(value))
            return mapping[lowered]
        return int(value)

    def _set_track_crossfade_assign(self, track_index, value, track_scope="track"):
        """Set a track mixer-device crossfade assignment."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.mixer_device.crossfade_assign = self._coerce_crossfade_assign(value)
            return self._get_track_mixer(track_index, track_scope)
        except Exception as e:
            self.log_message("Error setting track crossfade assign: " + str(e))
            raise

    def _coerce_panning_mode(self, value):
        """Map readable panning mode names to integer values."""
        if isinstance(value, string_types):
            mapping = {
                "stereo": 0,
                "balance": 0,
                "split": 1,
                "split_stereo": 1,
                "split stereo": 1
            }
            lowered = value.strip().lower()
            if lowered not in mapping:
                raise ValueError("Unknown panning mode '{0}'".format(value))
            return mapping[lowered]
        return int(value)

    def _set_track_panning_mode(self, track_index, value, track_scope="track"):
        """Set a track mixer-device panning mode when supported."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.mixer_device.panning_mode = self._coerce_panning_mode(value)
            return self._get_track_mixer(track_index, track_scope)
        except Exception as e:
            self.log_message("Error setting track panning mode: " + str(e))
            raise

    def _set_track_fold_state(self, track_index, value, track_scope="track"):
        """Set the fold-state on foldable tracks when supported."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.fold_state = bool(value)
            return self._get_track_info(track_index, track_scope)
        except Exception as e:
            self.log_message("Error setting track fold state: " + str(e))
            raise

    def _set_track_showing_chains(self, track_index, enabled, track_scope="track"):
        """Show or hide chain view on a track when supported."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.is_showing_chains = bool(enabled)
            return self._get_track_info(track_index, track_scope)
        except Exception as e:
            self.log_message("Error setting track chain visibility: " + str(e))
            raise

    def _set_track_collapsed(self, track_index, enabled, track_scope="track"):
        """Collapse or expand a track's view when supported."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.view.is_collapsed = bool(enabled)
            return self._get_track_view(track_index, track_scope)
        except Exception as e:
            self.log_message("Error setting track collapsed state: " + str(e))
            raise

    def _set_track_device_insert_mode(self, track_index, enabled, track_scope="track"):
        """Enable or disable device insert mode for a track view."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.view.device_insert_mode = bool(enabled)
            return self._get_track_view(track_index, track_scope)
        except Exception as e:
            self.log_message("Error setting track device insert mode: " + str(e))
            raise

    def _set_track_send(self, track_index, send_index, value, track_scope="track"):
        """Set a send level on a track or group track."""
        try:
            track, parameter, send_name = self._resolve_send_parameter(track_index, send_index, track_scope)
            self._set_parameter_value(parameter, value)

            return {
                "track_name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "send_index": int(send_index),
                "send_name": send_name,
                "value": parameter.value,
                "display_value": getattr(parameter, "display_value", None),
                "automation_state": getattr(parameter, "automation_state", None)
            }
        except Exception as e:
            self.log_message("Error setting track send: " + str(e))
            raise

    def _set_song_time(self, time_value):
        """Move the arrangement playhead to a beat time."""
        try:
            self._song.current_song_time = float(time_value)
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error setting song time: " + str(e))
            raise

    def _set_song_record_mode(self, enabled):
        """Enable or disable arrangement record mode."""
        try:
            self._song.record_mode = bool(enabled)
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error setting song record mode: " + str(e))
            raise

    def _set_song_arrangement_overdub(self, enabled):
        """Enable or disable arrangement overdub."""
        try:
            self._song.arrangement_overdub = bool(enabled)
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error setting arrangement overdub: " + str(e))
            raise

    def _set_song_session_automation_record(self, enabled):
        """Enable or disable session automation record."""
        try:
            self._song.session_automation_record = bool(enabled)
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error setting session automation record: " + str(e))
            raise

    def _set_song_overdub(self, enabled):
        """Enable or disable song overdub."""
        try:
            self._song.overdub = bool(enabled)
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error setting overdub: " + str(e))
            raise

    def _set_song_loop(self, enabled, start_time=None, length=None):
        """Enable or disable the arrangement loop."""
        try:
            if start_time is not None:
                self._song.loop_start = float(start_time)
            if length is not None:
                self._song.loop_length = float(length)
            self._song.loop = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting song loop: " + str(e))
            raise

    def _set_song_loop_start(self, value):
        """Set arrangement loop start."""
        try:
            self._song.loop_start = float(value)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting song loop start: " + str(e))
            raise

    def _set_song_loop_length(self, value):
        """Set arrangement loop length."""
        try:
            self._song.loop_length = float(value)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting song loop length: " + str(e))
            raise

    def _set_song_metronome(self, enabled):
        """Enable or disable the metronome."""
        try:
            self._song.metronome = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting metronome: " + str(e))
            raise

    def _set_song_signature(self, numerator, denominator):
        """Set the song time signature."""
        try:
            self._song.signature_numerator = int(numerator)
            self._song.signature_denominator = int(denominator)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting song signature: " + str(e))
            raise

    def _set_song_exclusive_arm(self, enabled):
        """Set Live's exclusive-arm preference for the current song."""
        try:
            self._song.exclusive_arm = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting exclusive arm: " + str(e))
            raise

    def _set_song_exclusive_solo(self, enabled):
        """Set Live's exclusive-solo preference for the current song."""
        try:
            self._song.exclusive_solo = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting exclusive solo: " + str(e))
            raise

    def _set_song_groove_amount(self, value):
        """Set global groove amount."""
        try:
            self._song.groove_amount = float(value)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting groove amount: " + str(e))
            raise

    def _set_song_swing_amount(self, value):
        """Set global swing amount."""
        try:
            self._song.swing_amount = float(value)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting swing amount: " + str(e))
            raise

    def _set_song_root_note(self, value):
        """Set the song scale root note."""
        try:
            self._song.root_note = int(value)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting root note: " + str(e))
            raise

    def _set_song_scale_name(self, value):
        """Set the song scale name."""
        try:
            self._song.scale_name = value
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting scale name: " + str(e))
            raise

    def _set_song_scale_mode(self, enabled):
        """Enable or disable scale mode."""
        try:
            self._song.scale_mode = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting scale mode: " + str(e))
            raise

    def _set_song_clip_trigger_quantization(self, value):
        """Set clip trigger quantization by integer enum."""
        try:
            self._song.clip_trigger_quantization = int(value)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting clip trigger quantization: " + str(e))
            raise

    def _set_song_midi_recording_quantization(self, value):
        """Set MIDI recording quantization by integer enum."""
        try:
            self._song.midi_recording_quantization = int(value)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting MIDI recording quantization: " + str(e))
            raise

    def _set_song_punch_in(self, enabled):
        """Enable or disable punch-in."""
        try:
            self._song.punch_in = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting punch in: " + str(e))
            raise

    def _set_song_punch_out(self, enabled):
        """Enable or disable punch-out."""
        try:
            self._song.punch_out = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting punch out: " + str(e))
            raise

    def _set_song_link_enabled(self, enabled):
        """Enable or disable Ableton Link."""
        try:
            self._song.is_ableton_link_enabled = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting Ableton Link: " + str(e))
            raise

    def _set_song_link_start_stop_sync(self, enabled):
        """Enable or disable Link start/stop sync."""
        try:
            self._song.is_ableton_link_start_stop_sync_enabled = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting Link start/stop sync: " + str(e))
            raise

    def _set_song_tempo_follower_enabled(self, enabled):
        """Enable or disable tempo follower."""
        try:
            self._song.tempo_follower_enabled = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting tempo follower: " + str(e))
            raise

    def _set_song_nudge_up(self, enabled):
        """Enable or disable nudge up."""
        try:
            self._song.nudge_up = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting nudge up: " + str(e))
            raise

    def _set_song_nudge_down(self, enabled):
        """Enable or disable nudge down."""
        try:
            self._song.nudge_down = bool(enabled)
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error setting nudge down: " + str(e))
            raise

    def _tap_tempo(self):
        """Tap Live's tempo."""
        try:
            self._song.tap_tempo()
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error tapping tempo: " + str(e))
            raise

    def _fire_scene(self, scene_index):
        """Launch a scene by index."""
        try:
            scene = self._resolve_scene(scene_index)
            scene.fire()
            return self._get_scene_info(scene_index)
        except Exception as e:
            self.log_message("Error firing scene: " + str(e))
            raise

    def _fire_scene_as_selected(self, scene_index):
        """Select and launch a scene as the selected scene."""
        try:
            scene = self._resolve_scene(scene_index)
            self._song.view.selected_scene = scene
            scene.fire_as_selected()
            return {
                "scene": self._get_scene_info(scene_index),
                "view_state": self._get_view_state()
            }
        except Exception as e:
            self.log_message("Error firing selected scene: " + str(e))
            raise

    def _stop_all_clips(self):
        """Stop all currently playing clips."""
        try:
            self._song.stop_all_clips()
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error stopping all clips: " + str(e))
            raise

    def _jump_to_cue_point(self, cue_index):
        """Jump to a cue point by index."""
        try:
            cue_points = list(getattr(self._song, "cue_points", []))
            cue_index = int(cue_index)
            if cue_index < 0 or cue_index >= len(cue_points):
                raise IndexError("Cue point index out of range")
            cue_point = cue_points[cue_index]
            try:
                cue_point.jump()
            except Exception:
                self._song.current_song_time = float(getattr(cue_point, "time", 0.0))
            return {
                "cue_point": self._describe_cue_point(cue_point, cue_index),
                "song_state": self._get_song_state()
            }
        except Exception as e:
            self.log_message("Error jumping to cue point: " + str(e))
            raise

    def _jump_to_next_cue(self):
        """Jump to the next cue point."""
        try:
            self._song.jump_to_next_cue()
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error jumping to next cue: " + str(e))
            raise

    def _jump_to_prev_cue(self):
        """Jump to the previous cue point."""
        try:
            self._song.jump_to_prev_cue()
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error jumping to previous cue: " + str(e))
            raise

    def _set_or_delete_cue(self):
        """Toggle a cue point at the current song time."""
        try:
            self._song.set_or_delete_cue()
            return self._get_cue_points()
        except Exception as e:
            self.log_message("Error toggling cue point: " + str(e))
            raise

    def _undo(self):
        """Undo the last Live action."""
        try:
            self._song.undo()
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error undoing: " + str(e))
            raise

    def _redo(self):
        """Redo the last undone Live action."""
        try:
            self._song.redo()
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error redoing: " + str(e))
            raise

    def _capture_and_insert_scene(self):
        """Capture playing clips into a new scene."""
        try:
            self._song.capture_and_insert_scene()
            return self._get_scenes()
        except Exception as e:
            self.log_message("Error capturing scene: " + str(e))
            raise

    def _trigger_session_record(self):
        """Trigger session recording."""
        try:
            self._song.trigger_session_record()
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error triggering session record: " + str(e))
            raise

    def _capture_midi(self):
        """Capture MIDI into a clip when available."""
        try:
            self._song.capture_midi()
            return self._get_song_overview()
        except Exception as e:
            self.log_message("Error capturing MIDI: " + str(e))
            raise

    def _continue_playing(self):
        """Continue playback from the stop point."""
        try:
            self._song.continue_playing()
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error continuing playback: " + str(e))
            raise

    def _play_selection(self):
        """Play the current arrangement selection."""
        try:
            self._song.play_selection()
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error playing selection: " + str(e))
            raise

    def _jump_by(self, beats):
        """Jump the arrangement playhead by beats."""
        try:
            self._song.jump_by(float(beats))
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error jumping by beats: " + str(e))
            raise

    def _scrub_by(self, beats):
        """Scrub the arrangement playhead by beats."""
        try:
            self._song.scrub_by(float(beats))
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error scrubbing by beats: " + str(e))
            raise

    def _normalize_view_direction(self, direction):
        """Convert readable direction names into Live's integer constants."""
        if isinstance(direction, string_types):
            needle = direction.strip().lower()
            mapping = {
                "up": 0,
                "down": 1,
                "left": 2,
                "right": 3
            }
            if needle not in mapping:
                raise ValueError("Unknown direction '{0}'".format(direction))
            return mapping[needle]
        return int(direction)

    def _select_track(self, track_index, track_scope="track"):
        """Select a track in Live's UI."""
        track = self._resolve_track(track_index, track_scope)
        self._song.view.selected_track = track
        return {
            "selected_track": self._resolve_track_reference(track),
            "view_state": self._get_view_state()
        }

    def _select_scene(self, scene_index):
        """Select a scene in Live's UI."""
        scene = self._resolve_scene(scene_index)
        self._song.view.selected_scene = scene
        return {
            "selected_scene": self._resolve_scene_reference(scene),
            "view_state": self._get_view_state()
        }

    def _select_track_instrument(self, track_index, track_scope="track"):
        """Select a track's instrument in the device view."""
        track = self._resolve_track(track_index, track_scope)
        track.view.select_instrument()
        return {
            "selected_track": self._resolve_track_reference(track),
            "view": self._get_track_view_summary(track),
            "view_state": self._get_view_state()
        }

    def _select_device(self, track_index, device_index, track_scope="track", container_path=None):
        """Select a device in Live's UI."""
        track = self._resolve_track(track_index, track_scope)
        _, device = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
        self._song.view.select_device(device)
        return {
            "selected_track": self._resolve_track_reference(track),
            "selected_device": self._resolve_device_reference(device),
            "view": self._get_track_view_summary(track),
            "view_state": self._get_view_state()
        }

    def _select_clip_slot(self, track_index, clip_index, track_scope="track"):
        """Select a clip slot in Live's UI."""
        track, clip_slot = self._resolve_clip_slot(track_index, clip_index, track_scope)
        self._song.view.highlighted_clip_slot = clip_slot
        return {
            "selected_track": self._resolve_track_reference(track),
            "highlighted_clip_slot": self._resolve_clip_slot_reference(clip_slot),
            "view_state": self._get_view_state()
        }

    def _fire_clip_slot(self, track_index, clip_index, track_scope="track"):
        """Fire a clip slot, whether or not it currently contains a clip."""
        _, clip_slot = self._resolve_clip_slot(track_index, clip_index, track_scope)
        clip_slot.fire()
        return self._serialize_clip_slot(clip_slot, int(track_index), int(clip_index))

    def _select_parameter(self, track_index, device_index, parameter_name,
                          track_scope="track", container_path=None,
                          parameter_source="device", send_index=0):
        """Select a device or mixer parameter in Live's UI."""
        track = self._resolve_track(track_index, track_scope)
        _, parameter, parameter_info = self._resolve_clip_automation_parameter(
            track_index,
            device_index,
            parameter_name,
            track_scope,
            container_path,
            parameter_source,
            send_index
        )
        self._song.view.selected_track = track
        self._song.view.selected_parameter = parameter
        return {
            "selected_track": self._resolve_track_reference(track),
            "selected_parameter": self._describe_parameter(parameter),
            "parameter_info": parameter_info,
            "view_state": self._get_view_state()
        }

    def _show_view(self, view_name):
        """Show a named Live view."""
        self.application().view.show_view(view_name)
        return self._get_view_state()

    def _focus_view(self, view_name):
        """Focus a named Live view."""
        self.application().view.focus_view(view_name)
        return self._get_view_state()

    def _hide_view(self, view_name):
        """Hide a named Live view."""
        self.application().view.hide_view(view_name)
        return self._get_view_state()

    def _scroll_view(self, direction, view_name="", modifier_pressed=False, amount=1):
        """Scroll a Live view one or more steps."""
        amount = max(1, int(amount))
        direction = self._normalize_view_direction(direction)
        for _ in range(amount):
            self.application().view.scroll_view(direction, view_name, int(bool(modifier_pressed)))
        return self._get_view_state()

    def _zoom_view(self, direction, view_name="", modifier_pressed=False, amount=1):
        """Zoom a Live view one or more steps."""
        amount = max(1, int(amount))
        direction = self._normalize_view_direction(direction)
        for _ in range(amount):
            self.application().view.zoom_view(direction, view_name, int(bool(modifier_pressed)))
        return self._get_view_state()

    def _set_draw_mode(self, enabled):
        """Enable or disable draw mode."""
        self._song.view.draw_mode = bool(enabled)
        return self._get_view_state()

    def _set_follow_song(self, enabled):
        """Enable or disable Follow Song."""
        self._song.view.follow_song = bool(enabled)
        return self._get_view_state()

    def _re_enable_automation(self):
        """Re-enable arrangement automation after manual overrides."""
        try:
            self._song.re_enable_automation()
            return self._get_song_state()
        except Exception as e:
            self.log_message("Error re-enabling automation: " + str(e))
            raise

    def _record_track_send_automation(self, track_index, send_index, points, track_scope="track",
                                      pre_roll_beats=0.125, settle_seconds=0.03,
                                      poll_interval_seconds=0.01, max_segment_gap_beats=8.0,
                                      restore_transport=True):
        """Write arrangement send automation by recording live parameter moves."""
        try:
            if track_scope != "track":
                raise ValueError("record_track_send_automation currently supports regular tracks only")

            track, parameter, send_name = self._resolve_send_parameter(track_index, send_index, track_scope)
            if not points:
                raise ValueError("No automation points provided")

            normalized_points = []
            for point in points:
                if "time" not in point or "value" not in point:
                    raise ValueError("Each automation point must include 'time' and 'value'")
                clamped_value = max(parameter.min, min(parameter.max, float(point["value"])))
                normalized_points.append({
                    "time": float(point["time"]),
                    "value": clamped_value
                })

            normalized_points = sorted(normalized_points, key=lambda item: item["time"])
            if len(normalized_points) < 2:
                raise ValueError("At least two automation points are required")

            pre_roll_beats = max(0.0, float(pre_roll_beats))
            settle_seconds = max(0.0, float(settle_seconds))
            poll_interval_seconds = max(0.001, float(poll_interval_seconds))
            max_segment_gap_beats = max(0.0, float(max_segment_gap_beats))

            segments = []
            current_segment = [normalized_points[0]]
            for point in normalized_points[1:]:
                if point["time"] - current_segment[-1]["time"] <= max_segment_gap_beats:
                    current_segment.append(point)
                else:
                    segments.append(current_segment)
                    current_segment = [point]
            segments.append(current_segment)

            original_state = self._get_song_state()
            if original_state["is_playing"]:
                self._song.stop_playing()
                time.sleep(settle_seconds)

            self._song.record_mode = True
            self._song.arrangement_overdub = True

            recorded_changes = 0
            for segment in segments:
                start_time = max(0.0, segment[0]["time"] - pre_roll_beats)
                self._song.current_song_time = start_time
                time.sleep(settle_seconds)

                # Prime the send to the segment's starting value before entering record.
                self._set_parameter_value(parameter, segment[0]["value"])
                time.sleep(settle_seconds)

                self._song.start_playing()
                time.sleep(settle_seconds)

                for point in segment[1:]:
                    while self._song.current_song_time < point["time"]:
                        time.sleep(poll_interval_seconds)
                    self._set_parameter_value(parameter, point["value"])
                    recorded_changes += 1
                    time.sleep(settle_seconds)

                self._song.stop_playing()
                time.sleep(settle_seconds)

            if restore_transport:
                self._song.current_song_time = original_state["current_song_time"]
                time.sleep(settle_seconds)
                if original_state["is_playing"]:
                    self._song.start_playing()
                else:
                    self._song.stop_playing()

            self._song.record_mode = original_state["record_mode"]
            self._song.arrangement_overdub = original_state["arrangement_overdub"]

            return {
                "track_name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "send_index": int(send_index),
                "send_name": send_name,
                "segments_recorded": len(segments),
                "input_point_count": len(normalized_points),
                "recorded_change_count": recorded_changes,
                "song_state": self._get_song_state()
            }
        except Exception as e:
            self.log_message("Error recording track send automation: " + str(e))
            raise

    def _set_track_output_routing(self, track_index, routing_name, sub_routing_name="", track_scope="track"):
        """Set a track's output routing and optional sub-routing by readable names."""
        try:
            track = self._resolve_track(track_index, track_scope)

            routing_option = self._match_routing_option(track.available_output_routing_types, routing_name)
            if routing_option is None:
                raise ValueError("Output routing '{0}' not found".format(routing_name))

            try:
                track.output_routing_type = routing_option
            except Exception:
                track.current_output_routing = routing_option

            if sub_routing_name:
                sub_option = self._match_routing_option(track.available_output_routing_channels, sub_routing_name)
                if sub_option is None:
                    raise ValueError("Output sub-routing '{0}' not found".format(sub_routing_name))
                try:
                    track.output_routing_channel = sub_option
                except Exception:
                    track.current_output_sub_routing = sub_option

            return {
                "track_name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "routing": self._get_track_routing_summary(track)
            }
        except Exception as e:
            self.log_message("Error setting track output routing: " + str(e))
            raise

    def _set_track_input_routing(self, track_index, routing_name, sub_routing_name="", track_scope="track"):
        """Set a track's input routing and optional sub-routing by readable names."""
        try:
            track = self._resolve_track(track_index, track_scope)

            routing_option = self._match_routing_option(track.available_input_routing_types, routing_name)
            if routing_option is None:
                raise ValueError("Input routing '{0}' not found".format(routing_name))

            try:
                track.input_routing_type = routing_option
            except Exception:
                track.current_input_routing = routing_option

            if sub_routing_name:
                sub_option = self._match_routing_option(track.available_input_routing_channels, sub_routing_name)
                if sub_option is None:
                    raise ValueError("Input sub-routing '{0}' not found".format(sub_routing_name))
                try:
                    track.input_routing_channel = sub_option
                except Exception:
                    track.current_input_sub_routing = sub_option

            return {
                "track_name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "routing": self._get_track_routing_summary(track)
            }
        except Exception as e:
            self.log_message("Error setting track input routing: " + str(e))
            raise

    def _set_track_monitoring_state(self, track_index, state, track_scope="track"):
        """Set a track's monitoring state."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.current_monitoring_state = int(state)
            return {
                "track_name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "monitoring": self._get_track_monitoring_summary(track)
            }
        except Exception as e:
            self.log_message("Error setting track monitoring state: " + str(e))
            raise

    def _set_master_volume(self, value):
        """Set the master track volume value on the mixer device."""
        try:
            parameter = self._song.master_track.mixer_device.volume
            clamped = max(parameter.min, min(parameter.max, float(value)))
            parameter.value = clamped

            return {
                "track_name": "Master",
                "volume": parameter.value
            }
        except Exception as e:
            self.log_message("Error setting master volume: " + str(e))
            raise

    def _set_master_cue_volume(self, value):
        """Set the master cue volume when exposed by the mixer device."""
        try:
            parameter = self._song.master_track.mixer_device.cue_volume
            self._set_parameter_value(parameter, value)
            return {
                "track_name": "Master",
                "cue_volume": parameter.value,
                "display_value": getattr(parameter, "display_value", None)
            }
        except Exception as e:
            self.log_message("Error setting master cue volume: " + str(e))
            raise

    def _set_master_crossfader(self, value):
        """Set the master crossfader when exposed by the mixer device."""
        try:
            parameter = self._song.master_track.mixer_device.crossfader
            self._set_parameter_value(parameter, value)
            return {
                "track_name": "Master",
                "crossfader": parameter.value,
                "display_value": getattr(parameter, "display_value", None)
            }
        except Exception as e:
            self.log_message("Error setting master crossfader: " + str(e))
            raise

    def _delete_track(self, track_index):
        """Delete a track by index from the Live set."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            try:
                deleted_name = track.name
            except Exception:
                deleted_name = ""

            # Best-effort prep for tracks Live may reject while armed/active.
            try:
                track.stop_all_clips()
            except Exception:
                pass
            try:
                if track.arm:
                    track.arm = False
            except Exception:
                pass
            try:
                self._song.view.selected_track = track
            except Exception:
                pass

            self._song.delete_track(track_index)

            result = {
                "deleted_index": track_index,
                "deleted_name": deleted_name
            }
            return result
        except Exception as e:
            self.log_message("Error deleting track: " + str(e))
            raise

    def _delete_device(self, track_index, device_index, track_scope="track", container_path=None):
        """Delete a device from a track or the master track."""
        try:
            container, device = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
            device_name = device.name
            if not hasattr(container, "delete_device"):
                raise ValueError("Resolved container cannot delete devices")
            container.delete_device(device_index)

            return {
                "track_name": getattr(container, "name", ""),
                "track_scope": track_scope,
                "container_path": container_path or [],
                "device_index": device_index,
                "device_name": device_name
            }
        except Exception as e:
            self.log_message("Error deleting device: " + str(e))
            raise

    def _duplicate_device(self, track_index, device_index, track_scope="track", container_path=None):
        """Duplicate a device on a track or the master track."""
        try:
            container, device = self._resolve_device_in_container(track_index, track_scope, container_path, device_index)
            device_name = device.name
            if not hasattr(container, "duplicate_device"):
                raise ValueError("Resolved container cannot duplicate devices")
            container.duplicate_device(device_index)

            return {
                "track_name": getattr(container, "name", ""),
                "track_scope": track_scope,
                "container_path": container_path or [],
                "source_device_index": device_index,
                "device_name": device_name,
                "duplicated_device_index": device_index + 1
            }
        except Exception as e:
            self.log_message("Error duplicating device: " + str(e))
            raise

    def _insert_device(self, track_index, track_scope="track", container_path=None, device_name="", target_index=None):
        """Insert a native device into a track or nested chain container."""
        try:
            container = self._resolve_container(track_index, track_scope, container_path)
            if not hasattr(container, "insert_device"):
                raise ValueError("Resolved container cannot insert devices")

            before_count = len(list(container.devices))
            if target_index is None:
                container.insert_device(device_name)
                inserted_index = before_count
            else:
                inserted_index = int(target_index)
                container.insert_device(device_name, inserted_index)

            return {
                "track_name": getattr(container, "name", ""),
                "track_scope": track_scope,
                "container_path": container_path or [],
                "device_name": device_name,
                "inserted_index": inserted_index,
                "device_count": len(list(container.devices))
            }
        except Exception as e:
            self.log_message("Error inserting device: " + str(e))
            raise

    def _move_device(self, source_track_index, source_track_scope, source_container_path, source_device_index,
                     target_track_index, target_track_scope, target_container_path, target_index):
        """Move a device between track or chain containers."""
        try:
            source_container, device = self._resolve_device_in_container(
                source_track_index,
                source_track_scope,
                source_container_path,
                source_device_index
            )
            target_container = self._resolve_container(
                target_track_index,
                target_track_scope,
                target_container_path
            )
            destination_index = int(target_index)
            if destination_index < 0:
                destination_index = len(list(target_container.devices))

            self._song.move_device(device, target_container, destination_index)

            return {
                "device_name": device.name,
                "source_track_name": getattr(source_container, "name", ""),
                "source_track_scope": source_track_scope,
                "source_container_path": source_container_path or [],
                "source_device_index": source_device_index,
                "target_track_name": getattr(target_container, "name", ""),
                "target_track_scope": target_track_scope,
                "target_container_path": target_container_path or [],
                "target_index": destination_index
            }
        except Exception as e:
            self.log_message("Error moving device: " + str(e))
            raise

    def _set_clip_color(self, track_index, clip_index, color):
        """Set the color of a Session View clip."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]
            if not clip_slot.has_clip:
                raise ValueError("Clip slot is empty")

            clip = clip_slot.clip
            clip.color = max(0, int(color))

            result = {
                "color": clip.color,
                "clip_name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip color: " + str(e))
            raise

    def _sync_track_media_colors(self, track_index, include_session=True, include_arrangement=True):
        """Copy a track's color onto all clips on that track."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            color = max(0, int(track.color))
            session_clips_updated = 0
            arrangement_clips_updated = 0

            if include_session:
                for slot in track.clip_slots:
                    if slot.has_clip:
                        slot.clip.color = color
                        session_clips_updated += 1

            if include_arrangement:
                try:
                    for clip in list(track.arrangement_clips):
                        clip.color = color
                        arrangement_clips_updated += 1
                except Exception:
                    pass

            result = {
                "track_index": track_index,
                "track_name": track.name,
                "color": color,
                "session_clips_updated": session_clips_updated,
                "arrangement_clips_updated": arrangement_clips_updated
            }
            return result
        except Exception as e:
            self.log_message("Error syncing track media colors: " + str(e))
            raise

    def _sync_all_media_colors(self, include_session=True, include_arrangement=True):
        """Copy each track's color onto its clips across the set."""
        try:
            tracks_updated = 0
            session_clips_updated = 0
            arrangement_clips_updated = 0

            for track_index in range(len(self._song.tracks)):
                result = self._sync_track_media_colors(
                    track_index,
                    include_session,
                    include_arrangement
                )
                tracks_updated += 1
                session_clips_updated += result["session_clips_updated"]
                arrangement_clips_updated += result["arrangement_clips_updated"]

            return {
                "tracks_updated": tracks_updated,
                "session_clips_updated": session_clips_updated,
                "arrangement_clips_updated": arrangement_clips_updated
            }
        except Exception as e:
            self.log_message("Error syncing all media colors: " + str(e))
            raise

    def _remove_arrangement_clip_from_track(self, track, clip):
        """Delete an arrangement clip from a track, falling back to muting if needed."""
        errors = []

        for args in [(clip,), (clip.start_time,)]:
            try:
                track.delete_clip(*args)
                return "deleted"
            except Exception as e:
                errors.append(str(e))

        try:
            clip.muted = True
            return "muted"
        except Exception as e:
            errors.append(str(e))

        raise RuntimeError("; ".join(errors))

    def _split_arrangement_audio_track_by_clip_name(self, track_index):
        """Split an audio track into per-clip-name arrangement layers."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            source_track = self._song.tracks[track_index]
            arrangement_clips = list(source_track.arrangement_clips)
            if not arrangement_clips:
                return {
                    "track_index": track_index,
                    "track_name": source_track.name,
                    "status": "no_arrangement_clips"
                }

            clip_names = []
            for clip in arrangement_clips:
                clip_name = clip.name or source_track.name
                if clip_name not in clip_names:
                    clip_names.append(clip_name)

            if len(clip_names) == 1:
                source_track.name = clip_names[0]
                return {
                    "track_index": track_index,
                    "track_name": source_track.name,
                    "status": "renamed_only",
                    "clip_names": clip_names
                }

            target_indices = [track_index]
            while len(target_indices) < len(clip_names):
                duplicate_source_index = target_indices[-1]
                self._song.duplicate_track(duplicate_source_index)
                target_indices.append(duplicate_source_index + 1)

            results = []
            removed_count = 0
            muted_count = 0

            for target_index, keep_name in zip(target_indices, clip_names):
                track = self._song.tracks[target_index]
                track.name = keep_name

                removed_here = 0
                muted_here = 0
                for clip in list(track.arrangement_clips):
                    clip_name = clip.name or keep_name
                    if clip_name == keep_name:
                        continue

                    action = self._remove_arrangement_clip_from_track(track, clip)
                    if action == "deleted":
                        removed_here += 1
                        removed_count += 1
                    elif action == "muted":
                        muted_here += 1
                        muted_count += 1

                results.append({
                    "track_index": target_index,
                    "track_name": track.name,
                    "kept_clip_name": keep_name,
                    "arrangement_clips_count": len(list(track.arrangement_clips)),
                    "removed_clips": removed_here,
                    "muted_clips": muted_here
                })

            return {
                "source_track_index": track_index,
                "source_track_name": source_track.name,
                "created_track_count": len(target_indices),
                "clip_names": clip_names,
                "removed_clips": removed_count,
                "muted_clips": muted_count,
                "tracks": results
            }
        except Exception as e:
            self.log_message("Error splitting arrangement audio track: " + str(e))
            raise
    
    def _create_clip(self, track_index, clip_index, length):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")
            
            # Create the clip
            clip_slot.create_clip(length)
            
            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise

    def _create_take_lane(self, track_index, track_scope="track"):
        """Create a take lane on a track when supported."""
        try:
            track = self._resolve_track(track_index, track_scope)
            lane = track.create_take_lane()
            return {
                "track_name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "created_lane_name": getattr(lane, "name", None),
                "take_lanes": self._get_take_lanes(track_index, track_scope).get("take_lanes", [])
            }
        except Exception as e:
            self.log_message("Error creating take lane: " + str(e))
            raise

    def _create_arrangement_audio_clip(self, track_index, file_path, position, track_scope="track"):
        """Create an arrangement audio clip on a track from a file path."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.create_audio_clip(str(file_path), float(position))
            return {
                "track_name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "position": float(position),
                "file_path": str(file_path),
                "arrangement_clips": self._get_arrangement_clips(track_index, track_scope)
            }
        except Exception as e:
            self.log_message("Error creating arrangement audio clip: " + str(e))
            raise

    def _create_arrangement_midi_clip(self, track_index, position, length, track_scope="track"):
        """Create an empty arrangement MIDI clip on a track."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.create_midi_clip(float(position), float(length))
            return {
                "track_name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "position": float(position),
                "length": float(length),
                "arrangement_clips": self._get_arrangement_clips(track_index, track_scope)
            }
        except Exception as e:
            self.log_message("Error creating arrangement MIDI clip: " + str(e))
            raise

    def _delete_clip_in_slot(self, track_index, clip_index, track_scope="track"):
        """Delete a clip from a specific clip slot."""
        try:
            track = self._resolve_track(track_index, track_scope)
            clip_index = int(clip_index)
            clip_slots = list(track.clip_slots)
            if clip_index < 0 or clip_index >= len(clip_slots):
                raise IndexError("Clip slot index out of range")
            slot = clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("Clip slot is empty")
            deleted_name = slot.clip.name
            track.delete_clip(clip_index)
            return {
                "track_name": getattr(track, "name", ""),
                "track_scope": track_scope,
                "clip_index": clip_index,
                "deleted_clip_name": deleted_name
            }
        except Exception as e:
            self.log_message("Error deleting clip in slot: " + str(e))
            raise

    def _duplicate_clip_slot(self, track_index, clip_index, track_scope="track"):
        """Duplicate a clip slot on the same track."""
        try:
            if track_scope != "track":
                raise ValueError("duplicate_clip_slot currently supports regular tracks only")
            track = self._resolve_track(track_index, track_scope)
            clip_index = int(clip_index)
            clip_slots = list(track.clip_slots)
            if clip_index < 0 or clip_index >= len(clip_slots):
                raise IndexError("Clip slot index out of range")
            track.duplicate_clip_slot(clip_index)
            return {
                "track_name": getattr(track, "name", ""),
                "clip_index": clip_index,
                "duplicated_clip_index": clip_index + 1
            }
        except Exception as e:
            self.log_message("Error duplicating clip slot: " + str(e))
            raise

    def _duplicate_clip_to_slot(self, source_track_index, source_clip_index, target_track_index, target_clip_index):
        """Report unsupported cross-track session-slot duplication explicitly."""
        raise ValueError("duplicate_clip_to_slot is not exposed natively; use duplicate_clip_slot on-track or duplicate_clip_to_arrangement")

    def _set_clip_slot_fire_button_state(self, track_index, clip_index, enabled, track_scope="track"):
        """Set a clip slot fire-button state."""
        try:
            track = self._resolve_track(track_index, track_scope)
            slot = list(track.clip_slots)[int(clip_index)]
            slot.set_fire_button_state(bool(enabled))
            return self._serialize_clip_slot(slot, track_index, int(clip_index))
        except Exception as e:
            self.log_message("Error setting clip slot fire button state: " + str(e))
            raise

    def _duplicate_clip_to_arrangement(self, track_index, clip_index, destination_time, track_scope="track"):
        """Duplicate a Session View clip to Arrangement View at a time in beats."""
        try:
            if track_scope != "track":
                raise ValueError("duplicate_clip_to_arrangement only supports regular tracks")

            track, clip = self._resolve_clip(track_index, clip_index, False, 0, track_scope)
            track.duplicate_clip_to_arrangement(clip, float(destination_time))

            return {
                "track_name": track.name,
                "track_scope": track_scope,
                "clip_name": clip.name,
                "clip_index": clip_index,
                "destination_time": float(destination_time),
                "duplicated": True
            }
        except Exception as e:
            self.log_message("Error duplicating clip to arrangement: " + str(e))
            raise
    
    def _add_notes_to_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            
            # Convert note data to Live's format
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                
                live_notes.append((pitch, start_time, duration, velocity, mute))
            
            # Add the notes
            clip.set_notes(tuple(live_notes))
            
            result = {
                "note_count": len(notes)
            }
            return result
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise
    
    def _set_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            clip.name = name
            
            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise
    
    def _set_tempo(self, tempo):
        """Set the tempo of the session"""
        try:
            self._song.tempo = tempo
            
            result = {
                "tempo": self._song.tempo
            }
            return result
        except Exception as e:
            self.log_message("Error setting tempo: " + str(e))
            raise
    
    def _fire_clip(self, track_index, clip_index):
        """Fire a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip_slot.fire()
            
            result = {
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise
    
    def _stop_clip(self, track_index, clip_index):
        """Stop a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            clip_slot.stop()
            
            result = {
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise

    def _jump_in_running_session_clip(self, track_index, beats, track_scope="track"):
        """Jump within the currently running session clip on a track."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.jump_in_running_session_clip(float(beats))
            return self._get_track_info(track_index, track_scope)
        except Exception as e:
            self.log_message("Error jumping in running session clip: " + str(e))
            raise

    def _stop_track_clips(self, track_index, track_scope="track"):
        """Stop all clips on a track-like object."""
        try:
            track = self._resolve_track(track_index, track_scope)
            track.stop_all_clips()
            return self._get_track_info(track_index, track_scope)
        except Exception as e:
            self.log_message("Error stopping track clips: " + str(e))
            raise

    
    def _start_playback(self):
        """Start playing the session"""
        try:
            self._song.start_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error starting playback: " + str(e))
            raise
    
    def _stop_playback(self):
        """Stop playing the session"""
        try:
            self._song.stop_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error stopping playback: " + str(e))
            raise
    
    def _get_browser_item(self, uri, path):
        """Get a browser item by URI or path"""
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            result = {
                "uri": uri,
                "path": path,
                "found": False
            }
            
            # Try to find by URI first if provided
            if uri:
                item = self._find_browser_item_by_uri(app.browser, uri)
                if item:
                    result["found"] = True
                    result["item"] = {
                        "name": item.name,
                        "is_folder": item.is_folder,
                        "is_device": item.is_device,
                        "is_loadable": item.is_loadable,
                        "uri": item.uri
                    }
                    return result
            
            # If URI not provided or not found, try by path
            if path:
                # Parse the path and navigate to the specified item
                path_parts = path.split("/")
                
                # Determine the root based on the first part
                current_item = None
                if path_parts[0].lower() == "nstruments":
                    current_item = app.browser.instruments
                elif path_parts[0].lower() == "sounds":
                    current_item = app.browser.sounds
                elif path_parts[0].lower() == "drums":
                    current_item = app.browser.drums
                elif path_parts[0].lower() == "audio_effects":
                    current_item = app.browser.audio_effects
                elif path_parts[0].lower() == "midi_effects":
                    current_item = app.browser.midi_effects
                else:
                    # Default to instruments if not specified
                    current_item = app.browser.instruments
                    # Don't skip the first part in this case
                    path_parts = ["instruments"] + path_parts
                
                # Navigate through the path
                for i in range(1, len(path_parts)):
                    part = path_parts[i]
                    if not part:  # Skip empty parts
                        continue
                    
                    found = False
                    for child in current_item.children:
                        if child.name.lower() == part.lower():
                            current_item = child
                            found = True
                            break
                    
                    if not found:
                        result["error"] = "Path part '{0}' not found".format(part)
                        return result
                
                # Found the item
                result["found"] = True
                result["item"] = {
                    "name": current_item.name,
                    "is_folder": current_item.is_folder,
                    "is_device": current_item.is_device,
                    "is_loadable": current_item.is_loadable,
                    "uri": current_item.uri
                }
            
            return result
        except Exception as e:
            self.log_message("Error getting browser item: " + str(e))
            self.log_message(traceback.format_exc())
            raise   
    
    
    
    def _load_browser_item(self, track_index, item_uri, track_scope="track",
                           selected_device_index=None, insert_mode=None):
        """Load a browser item onto a track by its URI"""
        try:
            track = self._resolve_track(track_index, track_scope)
            
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track
            try:
                if insert_mode is not None and hasattr(track, "view") and hasattr(track.view, "device_insert_mode"):
                    track.view.device_insert_mode = int(insert_mode)
                if selected_device_index is not None:
                    if selected_device_index < 0 or selected_device_index >= len(track.devices):
                        raise IndexError("Selected device index out of range")
                    self._song.view.select_device(track.devices[selected_device_index])
                elif len(track.devices) > 0:
                    self._song.view.select_device(track.devices[-1])
            except Exception:
                pass
            
            # Load the item
            app.browser.load_item(item)
            
            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "track_scope": track_scope,
                "selected_device_index": selected_device_index,
                "insert_mode": insert_mode,
                "uri": item_uri
            }
            return result
        except Exception as e:
            self.log_message("Error loading browser item: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI"""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item
            
            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None
            
            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                # Check all main categories
                categories = [
                    browser_or_item.instruments,
                    browser_or_item.sounds,
                    browser_or_item.drums,
                    browser_or_item.audio_effects,
                    browser_or_item.midi_effects
                ]
                
                for category in categories:
                    item = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if item:
                        return item
                
                return None
            
            # Check if this item has children
            if hasattr(browser_or_item, 'children') and browser_or_item.children:
                for child in browser_or_item.children:
                    item = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if item:
                        return item
            
            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: {0}".format(str(e)))
            return None
    
    # Helper methods
    
    def _get_device_type(self, device):
        """Get the type of a device"""
        try:
            # Simple heuristic - in a real implementation you'd look at the device class
            if device.can_have_drum_pads:
                return "drum_machine"
            elif device.can_have_chains:
                return "rack"
            elif "instrument" in device.class_display_name.lower():
                return "instrument"
            elif "audio_effect" in device.class_name.lower():
                return "audio_effect"
            elif "midi_effect" in device.class_name.lower():
                return "midi_effect"
            else:
                return "unknown"
        except:
            return "unknown"
    
    def get_browser_tree(self, category_type="all"):
        """
        Get a simplified tree of browser categories.
        
        Args:
            category_type: Type of categories to get ('all', 'instruments', 'sounds', etc.)
            
        Returns:
            Dictionary with the browser tree structure
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
            
            result = {
                "type": category_type,
                "categories": [],
                "available_categories": browser_attrs
            }
            
            # Helper function to process a browser item and its children
            def process_item(item, depth=0):
                if not item:
                    return None
                
                result = {
                    "name": item.name if hasattr(item, 'name') else "Unknown",
                    "is_folder": hasattr(item, 'children') and bool(item.children),
                    "is_device": hasattr(item, 'is_device') and item.is_device,
                    "is_loadable": hasattr(item, 'is_loadable') and item.is_loadable,
                    "uri": item.uri if hasattr(item, 'uri') else None,
                    "children": []
                }
                
                
                return result
            
            # Process based on category type and available attributes
            if (category_type == "all" or category_type == "instruments") and hasattr(app.browser, 'instruments'):
                try:
                    instruments = process_item(app.browser.instruments)
                    if instruments:
                        instruments["name"] = "Instruments"  # Ensure consistent naming
                        result["categories"].append(instruments)
                except Exception as e:
                    self.log_message("Error processing instruments: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "sounds") and hasattr(app.browser, 'sounds'):
                try:
                    sounds = process_item(app.browser.sounds)
                    if sounds:
                        sounds["name"] = "Sounds"  # Ensure consistent naming
                        result["categories"].append(sounds)
                except Exception as e:
                    self.log_message("Error processing sounds: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "drums") and hasattr(app.browser, 'drums'):
                try:
                    drums = process_item(app.browser.drums)
                    if drums:
                        drums["name"] = "Drums"  # Ensure consistent naming
                        result["categories"].append(drums)
                except Exception as e:
                    self.log_message("Error processing drums: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "audio_effects") and hasattr(app.browser, 'audio_effects'):
                try:
                    audio_effects = process_item(app.browser.audio_effects)
                    if audio_effects:
                        audio_effects["name"] = "Audio Effects"  # Ensure consistent naming
                        result["categories"].append(audio_effects)
                except Exception as e:
                    self.log_message("Error processing audio_effects: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "midi_effects") and hasattr(app.browser, 'midi_effects'):
                try:
                    midi_effects = process_item(app.browser.midi_effects)
                    if midi_effects:
                        midi_effects["name"] = "MIDI Effects"
                        result["categories"].append(midi_effects)
                except Exception as e:
                    self.log_message("Error processing midi_effects: {0}".format(str(e)))
            
            # Try to process other potentially available categories
            for attr in browser_attrs:
                if attr not in ['instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects'] and \
                   (category_type == "all" or category_type == attr):
                    try:
                        item = getattr(app.browser, attr)
                        if hasattr(item, 'children') or hasattr(item, 'name'):
                            category = process_item(item)
                            if category:
                                category["name"] = attr.capitalize()
                                result["categories"].append(category)
                    except Exception as e:
                        self.log_message("Error processing {0}: {1}".format(attr, str(e)))
            
            self.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result['categories'])))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser tree: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def get_browser_items_at_path(self, path):
        """
        Get browser items at a specific path.
        
        Args:
            path: Path in the format "category/folder/subfolder"
                 where category is one of: instruments, sounds, drums, audio_effects, midi_effects
                 or any other available browser category
                 
        Returns:
            Dictionary with items at the specified path
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
                
            # Parse the path
            path_parts = path.split("/")
            if not path_parts:
                raise ValueError("Invalid path")
            
            # Determine the root category
            root_category = path_parts[0].lower()
            current_item = None
            
            # Check standard categories first
            if root_category == "instruments" and hasattr(app.browser, 'instruments'):
                current_item = app.browser.instruments
            elif root_category == "sounds" and hasattr(app.browser, 'sounds'):
                current_item = app.browser.sounds
            elif root_category == "drums" and hasattr(app.browser, 'drums'):
                current_item = app.browser.drums
            elif root_category == "audio_effects" and hasattr(app.browser, 'audio_effects'):
                current_item = app.browser.audio_effects
            elif root_category == "midi_effects" and hasattr(app.browser, 'midi_effects'):
                current_item = app.browser.midi_effects
            else:
                # Try to find the category in other browser attributes
                found = False
                for attr in browser_attrs:
                    if attr.lower() == root_category:
                        try:
                            current_item = getattr(app.browser, attr)
                            found = True
                            break
                        except Exception as e:
                            self.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))
                
                if not found:
                    # If we still haven't found the category, return available categories
                    return {
                        "path": path,
                        "error": "Unknown or unavailable category: {0}".format(root_category),
                        "available_categories": browser_attrs,
                        "items": []
                    }
            
            # Navigate through the path
            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:  # Skip empty parts
                    continue
                
                if not hasattr(current_item, 'children'):
                    return {
                        "path": path,
                        "error": "Item at '{0}' has no children".format('/'.join(path_parts[:i])),
                        "items": []
                    }
                
                found = False
                for child in current_item.children:
                    if hasattr(child, 'name') and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                
                if not found:
                    return {
                        "path": path,
                        "error": "Path part '{0}' not found".format(part),
                        "items": []
                    }
            
            # Get items at the current path
            items = []
            if hasattr(current_item, 'children'):
                for child in current_item.children:
                    item_info = {
                        "name": child.name if hasattr(child, 'name') else "Unknown",
                        "is_folder": hasattr(child, 'children') and bool(child.children),
                        "is_device": hasattr(child, 'is_device') and child.is_device,
                        "is_loadable": hasattr(child, 'is_loadable') and child.is_loadable,
                        "uri": child.uri if hasattr(child, 'uri') else None
                    }
                    items.append(item_info)
            
            result = {
                "path": path,
                "name": current_item.name if hasattr(current_item, 'name') else "Unknown",
                "uri": current_item.uri if hasattr(current_item, 'uri') else None,
                "is_folder": hasattr(current_item, 'children') and bool(current_item.children),
                "is_device": hasattr(current_item, 'is_device') and current_item.is_device,
                "is_loadable": hasattr(current_item, 'is_loadable') and current_item.is_loadable,
                "items": items
            }
            
            self.log_message("Retrieved {0} items at path: {1}".format(len(items), path))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser items at path: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
