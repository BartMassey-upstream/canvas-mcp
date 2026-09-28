import assert from 'node:assert/strict';
import { test } from 'node:test';

import { canvasPath } from '../../src/canvas_mcp/code_api/client.js';

test('canvasPath encodes every dynamic value as one route segment', () => {
  assert.equal(
    canvasPath('courses', 'CS/101.11é', 'pages', 'question?fragment#'),
    '/courses/CS%2F101%2E11%C3%A9/pages/question%3Ffragment%23'
  );
  assert.equal(
    canvasPath('courses', 'sis_course_id:CS/101'),
    '/courses/sis_course_id:CS%2F101'
  );
  assert.equal(
    canvasPath('courses', '..', 'pages', 'already%2Fencoded'),
    '/courses/%2E%2E/pages/already%252Fencoded'
  );
});

test('canvasPath rejects empty segments', () => {
  assert.throws(() => canvasPath('courses', ''), /cannot be empty/);
});
