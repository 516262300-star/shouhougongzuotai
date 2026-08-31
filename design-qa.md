# 售后订单记录中心 Design QA

## 对比目标

- Source visual truth: `C:\Users\lds\.codex\generated_images\01a04c0f-21c7-7651-a92b-c7d0560e02ee\exec-cbb7b3e3-597e-4c3b-a3dc-63387cb45caa.png`
- Implementation screenshot: `D:\desktop\codex\售后工作台\design-qa-final.png`
- Full comparison: `D:\desktop\codex\售后工作台\design-qa-comparison-final.png`
- Focused table comparison: `D:\desktop\codex\售后工作台\design-qa-table-focus.png`
- Focused detail comparison: `D:\desktop\codex\售后工作台\design-qa-detail-focus.png`
- CSS viewport: 1536 × 1058, desktop, device scale factor 1.
- Source pixels: 1487 × 1058.
- Implementation capture pixels: 1536 × 1027. The in-app browser capture excludes 31 pixels of browser surface while the page reports a 1536 × 1058 CSS viewport; full-view evidence was normalized to 768 × 529 per side before judging composition.
- State: first page, 15 records per page, first order selected, right-side detail open, default seven-day date range.

## Full-view comparison evidence

The final comparison preserves the selected design's three-column hierarchy: 216-pixel navigation, dense central order workspace, and persistent 340-pixel detail/audit panel. The summary strip, two-row filter region, table start position, selected-row treatment, status colors, and detail section boundaries align with the source. Dynamic totals, shop names, order numbers, amounts, and workflow distribution intentionally come from the current MySQL data rather than the generated mock.

## Focused comparison evidence

- Table: `design-qa-table-focus.png` confirms that all ten columns are visible without horizontal scrolling, row density remains equivalent to the source, selected and semantic status states are readable, and the operation column is no longer clipped.
- Detail panel: `design-qa-detail-focus.png` confirms matching section order, label/value alignment, copy affordances, status treatment, and vertical audit timeline rhythm. The implementation contains longer live logistics copy, so it wraps more than the mock but remains within the panel.

## Required fidelity surfaces

- Fonts and typography: local Noto Sans SC Variable is used with compact 10.5–20 pixel hierarchy. Weights, line heights, truncation, and tabular-number behavior match the dense Chinese enterprise UI target.
- Spacing and layout rhythm: summary height is 88 pixels; the filter region is 113.5 pixels; the table begins at y=297.5 and uses 44-pixel rows. At 1536 pixels the table client width and scroll width are both 933 pixels. At 1280 pixels the detail becomes an overlay and the table client and scroll widths are both 1043 pixels.
- Colors and visual tokens: white and cool-gray surfaces, dark navy text, #1768d8 primary blue, and restrained green/orange/red/purple semantic states match the source intent. No gradients or decorative effects were added.
- Image quality and asset fidelity: the source contains no photos or illustration assets. Interface icons come from one Phosphor family; no placeholder imagery, handcrafted SVG, or CSS substitute for source imagery is present.
- Copy and content: all labels use concise Chinese business terminology. Live records expose the fields actually stored by the system; unavailable buyer nicknames are explicitly shown as “平台未返回”. Logistics timestamps written as UTC-naive values are converted to Asia/Shanghai before display, keeping the audit timeline chronological.
- Accessibility and states: controls are semantic buttons, selects, date inputs, and labeled icon buttons. Focus styles, selected rows, loading/error/empty states, disabled pagination, copy success, collapsed detail, and responsive overlay behavior are implemented.

## Comparison history

### Iteration 1 — blocked

- [P1] The table had a 990-pixel minimum width inside a 933-pixel content area, producing horizontal scrolling and clipping “查看详情”. Fixed by reducing the table minimum to 900 pixels, making column widths total 100%, and tightening cell padding.
- [P2] The initial filter panel was about 20 pixels taller than the source and pushed the table below the reference start position. Fixed by reducing vertical padding, label gaps, control height, and row gap; final table y-position is 297.5.
- [P2] At 1280 pixels, the persistent detail column left too little room for the dense table. Fixed by changing the detail panel to a 320-pixel overlay at that breakpoint while preserving the selected desktop layout at 1536 pixels.

### Iteration 2 — passed

- Post-fix browser evidence shows no body overflow, no table horizontal overflow at 1536 or 1280, 15 rendered rows, three detail sections, chronological timeline entries, and zero console errors.
- Primary interactions tested: logistics filter and reset, pagination, row-to-detail selection, close/reopen detail, and copy-success feedback.
- No actionable P0, P1, or P2 findings remain.

## Follow-up polish

- [P3] Exact row status distribution differs from the mock because the prototype intentionally renders live database state.
- [P3] The in-app browser's viewport screenshot excludes 31 pixels from the CSS viewport; DOM bounds confirm the bottom action bar is inside the declared viewport, and this does not affect normal browser rendering.

final result: passed
