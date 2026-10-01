# Agent Note: Keep publication results consistent with accepted answers

Status: implemented

## Problem

A revised answer could leave completed results publishable. Shared source keys could lose category-based format checks, published dictionaries were not checked against target formatting rules, and some output paths could not be represented by the manifest. CI gave the Agent the entire job deadline and published from the old triggering commit.

## Decision

Changed answers invalidate results and publication receipts before persistence. Translation finalization remains the only producer of results. Shared keys retain applicable category checks without adding another task format. Published strings use the same validation function; resource rules are checked only when local source snapshots exist. The manifest rejects reserved or conflicting paths before translation and during builds.

The default Agent timeout is 150 minutes inside a 180-minute job. Publication checks out the current branch head and applies the existing policy and dictionary conflict checks. A later push race still fails rather than force-pushing. No automatic retry scheduler or Agent session management is added.

## Alternatives considered

Rebuilding results during submit would simplify publishing after edits but bypass the explicit final snapshot check. Adding task categories and changing the plan schema would preserve every classification, but the existing merged rules can express the required checks. Fetching old sources during check would expand coverage at the cost of expensive game-dependent acquisition. Retrying and rebasing publication repeatedly would handle more races but complicate conflict handling; using the latest branch at publication handles ordinary intervening edits.

## Consequences

Existing published files and the Agent input/output protocol remain unchanged. Strict checks may expose old format errors that previously passed. Resource rules for absent historical snapshots cannot be verified. Regression tests cover corrections, shared category rules, existing placeholder/tag damage, and manifest collisions. Branch CI verifies the framework and a small real Agent translation before integration.

The game repository contains historical tag and newline differences. Full check reports them rather than rewriting them. CI uses check --changed-since against its base commit, and publication validates changed strings only, so unrelated updates remain possible without granting exceptions to new translations. Structure, term consistency, and manifests still cover the entire output.

An initial branch push without a before commit uses its parent as the comparison base. A repository's first commit still receives a full check.

## Verification

Template Actions run 36815809069 and game Actions run 36816068436 each translate and publish 27 keys from five resources using DeepSeek. Exact source keys, nested table paths, placeholders, and tags are checked after publication. The game run publishes on top of a documentation commit pushed after translation starts. Temporary configurations and output files are removed before integration.

The [source-keyed Agent interface](../architecture/2026-09-30-source-keyed-agent-batches.md) remains unchanged.
