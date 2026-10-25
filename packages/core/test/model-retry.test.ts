import { expect } from "bun:test"
import { AIError, ProviderInternalError, RateLimitError } from "@opencode/ai/effect"
import { ModelRetry } from "@opencode/core/model-retry"
import { Effect } from "effect"
import { it } from "./lib/effect"

it.effect("caps provider retry hints at 61 seconds for shared model calls", () =>
  Effect.gen(function* () {
    const decide = yield* ModelRetry.policy
    yield* Effect.forEach(
      [
        new RateLimitError({ message: "retry later", retryAfterMs: 3_600_000 }),
        new ProviderInternalError({ message: "retry later", retryAfterMs: 3_600_000 }),
      ],
      (reason) =>
        Effect.gen(function* () {
          expect(yield* decide({ cause: new AIError({ reason }), retry: true })).toMatchObject({
            retry: true,
            delay: 61_000,
          })
        }),
    )
  }),
)
