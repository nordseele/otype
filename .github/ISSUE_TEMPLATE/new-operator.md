---
name: New operator
about: Propose an OType operation with a precise device behavior and evidence
title: "Operator: "
labels: enhancement
---

## Proposed name and category
Use the naming rules in docs/adding-an-operator.md.

## Device behavior
Which button gesture or menu operation does it represent? Give the manual section.

## Arguments and limits
List defaults, ranges, clamping, and refused cases (including PICKUP and empty steps).

## Firmware evidence
Identify the stock routine or storage involved. Separate confirmed observations,
hypotheses, and unknowns. State which OS and hardware model were checked.

## Playback and task safety
Can it run while the sequencer plays? What proves it? Which task should run it?

## Example script
```text
# A minimal script showing the proposed operation
```

## Validation
Describe a test that fails without the change, emulator results, and hardware results.

An issue is a proposal, with no commitment to implement it or provide support.
