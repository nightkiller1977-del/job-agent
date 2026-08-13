# Job-Agent Response Loop Fix — Integration Guide

## Overview

This comprehensive solution addresses the job-agent response loop issue by:

1. **Centralizing Configuration** — All behavioral parameters (max_steps, timeouts, loop detection thresholds) are loaded from AI Commander's centralized settings (NO hard-coding)
2. **Implementing Progress Tracking** — Detects when the LLM agent is truly making progress vs. repeating the same state
3. **Improving LLM Prompts** — Provides explicit loop warnings and progress context to prevent repetitive behavior
4. **Adding Loop Detection** — Stops the agent before the step limit if a loop is detected, with clear reasoning
5. **Telemetry Integration** — Tracks loop events and step efficiency for monitoring and debugging

## Files Created

### 1. `src/agent_config.py`
**Configuration loading system with centralized parameter management**

- `ConfigLoader` class: Loads config from hierarchy (env → settings → config.json → defaults)
- Dataclasses for typed configuration
- Global `get_config()` and `reload_config()` functions
- NO hard-coded values — everything is configurable

**Key parameters:**
- `browser_recovery.max_steps`: Max LLM agent loop iterations (default: 8)
- `browser_recovery.step_timeout_ms`: Timeout per step (default: 15s)
- `browser_recovery.loop_detection.*`: Loop detection thresholds
- `browser_recovery.success_indicators`: Strings indicating successful submission
- `llm_prompting.*`: LLM behavior and context settings

**Usage:**
```python
from src.agent_config import get_config
config = get_config()
print(config.browser_recovery.max_steps)  # Access configuration
```

### 2. `src/progress_tracker.py`
**Progress tracking and loop detection engine**

- `ProgressTracker` class: Monitors form-filling state changes
- `DomState`: Snapshots of page DOM state for change detection
- `ActionRecord`: Records of LLM actions
- `LoopDetectionResult`: Analysis result with confidence scores

**Detects loops by:**
- Counting repeated DOM states (page hasn't changed)
- Counting repeated action sequences (same action type over and over)
- Tracking selector usage frequency (attempting same field repeatedly)
- Calculating progress score (0.0 to 1.0) based on state changes and action success

**Key methods:**
- `record_state()`: Record current page state
- `record_action()`: Record an action taken by LLM
- `detect_loop()`: Analyze and detect if agent is looping
- `get_progress_context()`: Get LLM-friendly progress summary

**Usage:**
```python
from src.progress_tracker import ProgressTracker

tracker = ProgressTracker(
    max_repeated_states=3,
    max_repeated_actions=2,
    min_progress_threshold=0.3,
)

# During agent loop:
tracker.record_state(body_text, title, element_count, form_field_count, has_submit)
loop_result = tracker.detect_loop()
if loop_result.is_looping:
    print(f"Loop detected: {loop_result.reason}")
    return failure(loop_result.reason)
```

### 3. `src/sources/adapters/recovery_browseruse_refactored.py`
**Refactored LLM browser recovery agent**

**Key improvements:**
- Integrates `ProgressTracker` for real-time loop detection
- Uses configuration from `agent_config` (no hard-coded max_steps, timeouts)
- Detects loops BEFORE exceeding step limit
- Enhanced LLM prompts with:
  - Progress visualization (step count, success rate, progress score)
  - Recent action history
  - Selector repetition warnings
  - Explicit guardrails against repeated actions
- Telemetry logging for loop events and step efficiency
- Better error messages with context

**Configuration-driven behavior:**
- Loop detection thresholds come from config
- Max steps from config
- Success/failure indicators from config
- LLM model task type from config

**Usage:**
```python
from src.sources.adapters.recovery_browseruse_refactored import BrowserUseRecoveryRefactored

adapter = BrowserUseRecoveryRefactored()
result = await adapter.apply(context)
```

## Integration with Orchestrator

### Step 1: Update `src/orchestrator.py`

Replace the import and router registration:

```python
# OLD (if any):
# from .sources.adapters.recovery_browseruse import BrowserUseRecovery

# NEW:
from .sources.adapters.recovery_browseruse_refactored import BrowserUseRecoveryRefactored
```

In the orchestrator's apply flow, use the refactored adapter:

```python
# Example: In the apply method where adapters are selected
if should_use_browser_recovery:
    adapter = BrowserUseRecoveryRefactored()   # config loaded from get_config()
    result = await adapter.apply(ctx)          # ctx is AtsApplyContext
```

### Step 2: Configuration in AI Commander

Add the `jobAgent` configuration to your `settings-v3.json`:

```json
{
  "jobAgent": {
    "browserRecovery": {
      "enabled": true,
      "maxSteps": 8,
      "stepTimeoutMs": 15000,
      "loopDetection": {
        "enabled": true,
        "maxRepeatedStates": 3,
        "maxRepeatedActions": 2,
        "stateHashWindow": 5,
        "progressCheckInterval": 2
      },
      "progressMetrics": {
        "trackDomChanges": true,
        "trackSelectorUsage": true,
        "trackActionSequence": true,
        "minProgressThreshold": 0.3
      },
      "successIndicators": [
        "thank you", "thanks", "thank", "received", "submitted",
        "success", "application sent", "application received", "confirmation"
      ],
      "failureIndicators": [
        "error", "failed", "cannot process", "invalid input",
        "required field", "not supported", "unsupported", "connection failed"
      ]
    },
    "llmPrompting": {
      "modelTask": "reasoning",
      "temperature": 0.3,
      "contextMode": "detailed",
      "progressVisualization": true,
      "explicitLoopGuards": true
    },
    "telemetry": {
      "trackLoopEvents": true,
      "trackStepEfficiency": true,
      "trackStateTransitions": true,
      "minDomChangeThreshold": 100
    }
  }
}
```

### Step 3: Environment Variable Overrides (Optional)

You can override any configuration via environment variables:

```bash
# Format: JOBAGENT_<SECTION>_<KEY>=value
export JOBAGENT_BROWSER_RECOVERY_MAX_STEPS=12
export JOBAGENT_BROWSER_RECOVERY_LOOP_DETECTION_ENABLED=true
export JOBAGENT_LLM_PROMPTING_TEMPERATURE=0.2

# Then run the agent
python src/main.py apply
```

### Step 4: Telemetry Integration

The refactored agent logs events that can be captured:

```python
# In your logging setup
import logging

# Configure logging to capture job-agent events
logging.getLogger("job-agent.adapters.recovery_browseruse").setLevel(logging.DEBUG)

# Log messages include:
# - "loop_detected domain=... step=... reason=..."
# - "loop_max_steps_exceeded domain=... max_steps=... progress=..."
# - "Loop completed successfully in N steps"
```

These can be sent to Loki or any centralized logging system via the existing telemetry pipeline.

## Testing the Solution

### Unit Tests

Test the configuration system:

```python
def test_config_loads_from_defaults():
    from src.agent_config import ConfigLoader
    loader = ConfigLoader()
    config = loader.load()
    assert config.browser_recovery.max_steps > 0
    assert config.browser_recovery.loop_detection.enabled

def test_config_environment_override():
    import os
    os.environ["JOBAGENT_BROWSER_RECOVERY_MAX_STEPS"] = "12"
    from src.agent_config import ConfigLoader
    loader = ConfigLoader(force_reload=True)
    config = loader.load()
    assert config.browser_recovery.max_steps == 12
```

Test the progress tracker:

```python
def test_loop_detection_repeated_states():
    from src.progress_tracker import ProgressTracker
    tracker = ProgressTracker(max_repeated_states=2)
    
    # Record same state 3 times
    for _ in range(3):
        tracker.record_state("same text", "title", 5, 3, True)
    
    result = tracker.detect_loop()
    assert result.is_looping
    assert result.repeated_states_count >= 2
```

Test the browser recovery agent:

```python
async def test_browser_recovery_detects_loop():
    from src.sources.adapters.recovery_browseruse_refactored import BrowserUseRecoveryRefactored
    
    adapter = BrowserUseRecoveryRefactored()
    # Mock context and page...
    result = await adapter.apply(mock_context)
    
    # Verify loop was detected before step limit
    assert result.status == "form_not_reached"
    assert "loop" in result.detail.lower()
```

### Integration Test

Run a real job application with the new system:

```bash
cd /path/to/job-agent

# Test with default configuration
python src/main.py apply --limit 1

# Test with custom configuration
export JOBAGENT_BROWSER_RECOVERY_MAX_STEPS=6
export JOBAGENT_BROWSER_RECOVERY_LOOP_DETECTION_ENABLED=true
python src/main.py apply --limit 1

# Check logs for loop detection events
grep -i "loop_detected" ~/.local/share/job-agent/logs/*
```

## Monitoring and Debugging

### Check Loop Events

```bash
# Query Loki for loop detection events
curl 'http://localhost:3100/loki/api/v1/query?query={application="ai-agents", level="warning"} |= "loop_detected"'
```

### Check Configuration State

```python
from src.agent_config import get_config
config = get_config()

print("Current Configuration:")
print(f"  Max Steps: {config.browser_recovery.max_steps}")
print(f"  Loop Detection: {config.browser_recovery.loop_detection.enabled}")
print(f"  Max Repeated States: {config.browser_recovery.loop_detection.max_repeated_states}")
print(f"  Progress Threshold: {config.browser_recovery.progress_metrics.min_progress_threshold}")
```

### Check Progress During Application

```python
# In the agent loop, access progress context
progress_context = tracker.get_progress_context()
print(f"Progress Score: {progress_context['progress_score']:.2f}")
print(f"Success Rate: {progress_context['success_rate']:.1%}")
print(f"Recent Actions: {progress_context['action_history_recent']}")
```

## Key Design Principles Applied

### 1. **No Hard-Coded Values**
- All behavioral parameters are configurable
- Configuration hierarchy: env → AI Commander settings → config.json → defaults
- Easy to adjust thresholds without code changes

### 2. **Reuse Existing Infrastructure**
- Uses AI Commander's centralized settings (not a parallel system)
- Integrates with existing ModelClient and telemetry
- Follows existing adapter pattern in job-agent

### 3. **Clear Failure Modes**
- Loop detection stops agent early with clear reasoning
- Error messages include context (progress score, repeated state count)
- Telemetry tracks all loop events for visibility

### 4. **Progress Tracking**
- Tracks real progress (DOM changes, field completions) vs. repetition
- Provides progress context to LLM for better decision-making
- Configurable thresholds for different form complexity

## Next Steps

1. **Copy files to main job-agent project**
2. **Update orchestrator.py to use refactored adapter**
3. **Add jobAgent configuration to settings-v3.json**
4. **Test with real job applications**
5. **Monitor telemetry for loop events**
6. **Adjust configuration based on production results**

## Support & Debugging

If the agent still loops after applying this fix:

1. **Check configuration loaded correctly:**
   ```python
   from src.agent_config import get_config
   config = get_config()
   print(config.browser_recovery)
   ```

2. **Lower the progress threshold:**
   ```bash
   export JOBAGENT_BROWSER_RECOVERY_PROGRESS_METRICS_MIN_PROGRESS_THRESHOLD=0.2
   ```

3. **Reduce max repeated states:**
   ```bash
   export JOBAGENT_BROWSER_RECOVERY_LOOP_DETECTION_MAX_REPEATED_STATES=2
   ```

4. **Check logs for loop detection events:**
   ```bash
   grep "loop_detected\|loop_max_steps_exceeded" logs/*
   ```

5. **Review recorded skills for the domain** (in `state/browseruse_skills.json`) — may need to be cleared if they contain incorrect sequences.
