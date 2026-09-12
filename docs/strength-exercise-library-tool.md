# Live Strength Builder exercise list

`tp_list_strength_exercises` is the account-scoped, read-only catalogue call for consumers that need the current full TrainingPeaks Strength Builder library rather than a name search.

It reads the live combined TrainingPeaks library only and returns built-in plus caller-owned custom exercises, muscle groups, block types, and the current exercise-parameter definitions. It deliberately has no baked fallback: a caller asking for the authoritative current catalogue must fail rather than silently lose custom exercises.

`tp_search_exercises` keeps its baked built-in fallback for interactive/search resilience. Custom exercise creation and update remain explicit real TrainingPeaks mutations through `tp_create_custom_exercise` / `tp_update_custom_exercise`.
