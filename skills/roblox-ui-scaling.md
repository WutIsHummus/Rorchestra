---
description: Roblox UI parenting, scaling, and positioning rules
triggers:
  runtime_sides: [client]
  scope_keywords: [Gui, UI, Frame, Button, TextLabel, ImageLabel, ImageButton, ScrollingFrame, TextButton, TextBox, ScreenGui, BillboardGui, SurfaceGui, UIPadding, UIListLayout, UIGridLayout, UICorner, UDim2]
---

# Roblox UI Scaling Rules

## Always Use Scale, Never Offset

- **Always** use scale-based `UDim2` values: `UDim2.new(scaleX, 0, scaleY, 0)`
- **Never** use pixel offsets: `UDim2.new(0, pixels, 0, pixels)` is forbidden
- This applies to `Size`, `Position`, `UIPadding`, `UIListLayout.Padding`, `UICorner.CornerRadius`, etc.

## How Parenting Affects Size and Position

Scale values are **relative to the parent element**, not the screen:

- `Size = UDim2.new(1, 0, 1, 0)` means 100% of the **parent's** width and height
- `Position = UDim2.new(0.5, 0, 0.5, 0)` means 50% of the **parent's** width/height from its top-left corner

### Key implications:

1. **Parent choice determines coordinate space**: A child with `Size = (0.5, 0.5)` inside a 100×100 parent is 50×50, but inside a 200×50 parent it's 100×25
2. **Thin parents need large height scales**: If a parent is very thin (e.g., a TextLabel with height `0.02` of screen), a child needs height `15` to appear `0.3` of the screen
3. **Wide parents need small width scales**: If a parent spans `0.8` of the screen, a child with `0.25` width is `0.2` of the screen
4. **Position is also relative**: `Position = (0.5, 0)` means horizontally centered in the parent, at the parent's top edge

### Before creating any UI element, always:

1. Check the parent's `Size` and `Position` to understand the coordinate space
2. Calculate what scale values produce the desired **screen-relative** dimensions
3. Use `AnchorPoint` to control which corner the position references
4. Account for the parent's aspect ratio when setting both width and height scales
