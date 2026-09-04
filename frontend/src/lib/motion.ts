// One shared spring config so every open/close interaction in the app -
// popovers, the panels drawer, the symbol dropdown - feels like the same
// physical system (doc/visual_reasoning_redesign.md: "consistent stiffness/
// damping across all of them").
export const SPRING = { type: "spring" as const, stiffness: 420, damping: 32, mass: 0.9 };

// A slightly snappier variant for small, quick elements (popover contents,
// dropdown items) - same character, less travel distance.
export const SPRING_SNAPPY = { type: "spring" as const, stiffness: 500, damping: 30, mass: 0.7 };
