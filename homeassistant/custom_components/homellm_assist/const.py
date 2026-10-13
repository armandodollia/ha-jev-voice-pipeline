"""Constants for Home LLM Assist."""

DOMAIN = "homellm_assist"

CONF_DECISION_URL = "decision_url"
CONF_FALLBACK_AGENT = "fallback_agent"
CONF_ACTION_THRESHOLD = "action_threshold"
CONF_DEVICE_THRESHOLD = "device_threshold"

DEFAULT_DECISION_URL = "http://192.168.1.33:8080/v1/decision"
DEFAULT_FALLBACK_AGENT = "conversation.local_llm_server_home_ai_agent"
DEFAULT_ACTION_THRESHOLD = 0.7
DEFAULT_DEVICE_THRESHOLD = 0.6

# What each spoken action does per domain. Domains or device classes not listed here are never handled by the
# fast path; those requests (and anything uncertain) go to the fallback agent.
ACTIONS = {
    "turn_on": {"light": "turn_on", "switch": "turn_on", "fan": "turn_on", "script": "turn_on", "cover": "open_cover"},
    "turn_off": {"light": "turn_off", "switch": "turn_off", "fan": "turn_off", "cover": "close_cover"},
    "open": {"cover": "open_cover"},
    "close": {"cover": "close_cover"},
}
# light settings (action -> light.turn_on with brightness_pct / color_name / color_temp_kelvin)
LIGHT_ACTIONS = ("set_brightness", "brighten", "dim", "set_color", "set_temperature")
# brightness the user can name: every 1% up to 20 (night-light levels), then steps of 5
BRIGHTNESS_STEPS = [*range(0, 21), *range(25, 101, 5)]
# relative brightness change in percentage points; words map to amounts, no amount means DEFAULT_STEP
STEP_WORDS = {"a_little": 10, "a_lot": 40}
STEP_AMOUNTS = ["5", "10", "15", "20", "25", "30", "40", "50"]
DEFAULT_STEP = 20
COLORS = ["red", "orange", "yellow", "green", "cyan", "blue", "purple", "magenta", "pink", "white"]
# spoken white temperatures; words map to Kelvin, numbers are Kelvin
KELVIN_WORDS = {"warm": 2700, "soft": 2700, "neutral": 4000, "cool": 5000, "daylight": 6500}
KELVIN_STEPS = ["2000", "2200", "2500", "2700", "3000", "3500", "4000", "4500", "5000", "5500", "6000", "6500"]
CONF_VALUE_THRESHOLD = "value_threshold"
DEFAULT_VALUE_THRESHOLD = 0.5

# security-sensitive covers stay with the fallback agent (and its confirmations)
EXCLUDED_COVER_CLASSES = {"garage", "gate", "door"}
