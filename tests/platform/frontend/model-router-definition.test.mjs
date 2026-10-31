// Isolated Router definition normalization tests. No browser, API or database.
import assert from "node:assert/strict"
import test from "node:test"

import {
  createRouterEdge,
  createRouterNode,
  normalizeRouterDefinition,
} from "../../../frontend/src/components/model-router/router-definition.ts"

const singleBranchCondition = (label) => {
  const start = createRouterNode("start", { x: 0, y: 0 })
  const condition = createRouterNode("condition", { x: 240, y: 0 })
  const output = createRouterNode("output", { x: 480, y: 0 })
  return {
    schema_version: 2,
    presentation_version: 6,
    nodes: [start, condition, output],
    edges: [
      createRouterEdge(start.id, condition.id),
      createRouterEdge(condition.id, output.id, label),
    ],
  }
}

test("a drafted Condition with only its Match branch survives reload", () => {
  const normalized = normalizeRouterDefinition(singleBranchCondition("Match"))

  assert.deepEqual(normalized.nodes.map((node) => node.type), ["start", "condition", "output"])
  assert.deepEqual(normalized.edges.map((edge) => edge.label), ["", "Match"])
})

test("a legacy unlabeled pass-through Condition is still collapsed", () => {
  const normalized = normalizeRouterDefinition(singleBranchCondition(""))

  assert.deepEqual(normalized.nodes.map((node) => node.type), ["start", "output"])
  assert.equal(normalized.edges.length, 1)
})
