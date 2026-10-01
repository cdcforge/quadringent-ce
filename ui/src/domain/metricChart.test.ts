import assert from 'node:assert/strict';
import test from 'node:test';
import { buildSparkline, sparklineToAreaPaths, sparklineToPaths } from './metricChart.ts';

test('an all-null series (e.g. a paused table) returns null, never a fabricated flat line at zero', () => {
  assert.equal(buildSparkline([null, null, null], 100, 40), null);
});

test('an empty series returns null', () => {
  assert.equal(buildSparkline([], 100, 40), null);
});

test('a single measured point still produces one segment with one point', () => {
  const sparkline = buildSparkline([5], 100, 40)!;
  assert.equal(sparkline.segments.length, 1);
  assert.equal(sparkline.segments[0]!.points.length, 1);
});

test('a null value in the middle of a series breaks it into two segments, never bridging the gap', () => {
  const sparkline = buildSparkline([1, null, 3], 100, 40)!;
  assert.equal(sparkline.segments.length, 2);
  assert.equal(sparkline.segments[0]!.points.length, 1);
  assert.equal(sparkline.segments[1]!.points.length, 1);
});

test('min/max reflect only the measured values, and 0 is always included in the range', () => {
  const sparkline = buildSparkline([10, 20, 30], 100, 40)!;
  assert.equal(sparkline.min, 0);
  assert.equal(sparkline.max, 30);
});

test('sparklineToPaths renders one SVG path per contiguous segment, moveto first then lineto', () => {
  const sparkline = buildSparkline([1, null, 3, 5], 100, 40)!;
  const paths = sparklineToPaths(sparkline);
  assert.equal(paths.length, 2);
  assert.match(paths[0]!, /^M/);
  assert.match(paths[1]!, /^M.*L/);
});

test('the plotted points stay inside a padded band — a peak never touches the very top or bottom edge', () => {
  const height = 40;
  const sparkline = buildSparkline([0, 100], 100, height)!;
  const ys = sparkline.segments.flatMap((segment) => segment.points.map((point) => point.y));
  for (const y of ys) {
    assert.ok(y > 0, `y=${y} touches the top edge`);
    assert.ok(y < height, `y=${y} touches the bottom edge`);
  }
});

test('sparklineToAreaPaths closes each segment down to the baseline, one area per contiguous segment', () => {
  const height = 40;
  const sparkline = buildSparkline([1, null, 3, 5], 100, height)!;
  const areas = sparklineToAreaPaths(sparkline, height);
  assert.equal(areas.length, 2);
  for (const area of areas) {
    assert.match(area, /Z$/);
    assert.match(area, new RegExp(`${height.toFixed(1)}`));
  }
});
