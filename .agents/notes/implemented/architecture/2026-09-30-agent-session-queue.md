# Agent Note: Keep Agent sessions separate from translation windows

Status: implemented

## Problem

The workflow used one model process for every packet. Packet size, recovery
checkpoint, and model session lifetime were the same concept, so a project
with many short stories paid the prompt, tool startup, and style context cost
dozens of times.

## Decision

The plan still creates bounded reading windows so the model receives a useful
amount of context and stable short numbers. One Agent session consumes the
whole pending queue by finishing a window and calling `next` for the next one.
Answers remain durable after every submission. A failed or timed out session
can be restarted and will receive only unfinished tasks. Complete scenes stay
intact; up to six short scenes may share a window, with advisory limits of 480
tasks or 32,000 source characters.

## Alternatives considered

Keeping one Agent session per packet would isolate failures, but repeats the
largest fixed costs and was the observed source of poor throughput. A
permanent service or parallel workers would add coordination and write-conflict
complexity without improving this single-target serial workflow.

## Consequences

Large jobs use one model startup per target and fewer repeated tool calls while
retaining durable recovery. A very long session may still hit model context or
runtime limits; the existing answer files make reruns safe. The packet field
remains an internal window identifier for validation and is not an Agent
session identifier.
