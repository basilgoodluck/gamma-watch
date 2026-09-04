"use client";

// doc/visual_reasoning_redesign.md item 3: a small inline SVG sparkline, not
// a chart library - no axes/legend, just the line shape.
export default function Sparkline({
  values,
  width = 120,
  height = 32,
  color,
}: {
  values: number[];
  width?: number;
  height?: number;
  color: string;
}) {
  if (values.length < 2) {
    return <svg width={width} height={height} />;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const step = width / (values.length - 1);

  const points = values.map((v, i) => {
    const x = i * step;
    const y = height - ((v - min) / range) * height;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  const last = values[values.length - 1];
  const lastX = (values.length - 1) * step;
  const lastY = height - ((last - min) / range) * height;

  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline points={points.join(" ")} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={lastX} cy={lastY} r={2} fill={color} />
    </svg>
  );
}
