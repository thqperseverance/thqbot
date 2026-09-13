Add-Type -AssemblyName System.Drawing

$repo = 'C:\Users\Administrator\Desktop\nanobot\nanobot-mvp'
$targets = @(
  @{ Path = "$repo\docs\poster.png";                  MinInk = 0.05; ExpectAccent = $true; Tight = $false },
  @{ Path = "$repo\docs\social-preview.png";          MinInk = 0.05; ExpectAccent = $true; Tight = $false },
  @{ Path = "$repo\docs\architecture-product.png";    MinInk = 0.05; ExpectAccent = $true; Tight = $false },
  @{ Path = "$repo\docs\architecture-tech.png";       MinInk = 0.05; ExpectAccent = $true; Tight = $false },
  # logo files are deliberately cropped flush to the mark, so edge contact is intended
  @{ Path = "$repo\docs\assets\logo-wordmark.png";    MinInk = 0.02; ExpectAccent = $true; Tight = $true }
)

$fails = 0
foreach ($t in $targets) {
  $bmp = [System.Drawing.Bitmap]::FromFile($t.Path)
  $w = $bmp.Width; $h = $bmp.Height
  $minX = $w; $minY = $h; $maxX = -1; $maxY = -1
  $cMinX = $w; $cMinY = $h; $cMaxX = -1; $cMaxY = -1
  $ink = 0; $accent = 0; $sampled = 0
  $edgeInk = 0
  for ($y = 0; $y -lt $h; $y += 2) {
    for ($x = 0; $x -lt $w; $x += 2) {
      $c = $bmp.GetPixel($x, $y)
      $sampled++
      $lum = 0.299 * $c.R + 0.587 * $c.G + 0.114 * $c.B
      # anything clearly brighter than the #0F0F11 page background counts as ink
      if ($lum -gt 32) {
        $ink++
        if ($x -lt $minX) { $minX = $x }; if ($x -gt $maxX) { $maxX = $x }
        if ($y -lt $minY) { $minY = $y }; if ($y -gt $maxY) { $maxY = $y }
        # accent-ish blue: strong B, moderate G, low R
        if ($c.B -gt 150 -and $c.B - $c.R -gt 60 -and $c.G -gt 90) { $accent++ }
      }
      # stricter: real drawn content (text, cards, bars), not the soft backdrop
      # glow; skip the top rows because the full-bleed brand bar lives there
      if ($lum -gt 70 -and $y -gt 10) {
        if ($x -lt $cMinX) { $cMinX = $x }; if ($x -gt $cMaxX) { $cMaxX = $x }
        if ($y -lt $cMinY) { $cMinY = $y }; if ($y -gt $cMaxY) { $cMaxY = $y }
      }
      # ink touching the very edge (the top 4px accent bar is intentional)
      if ($y -gt 8 -and ($x -le 1 -or $x -ge $w - 2 -or $y -ge $h - 2)) {
        if ($lum -gt 70) { $edgeInk++ }
      }
    }
  }
  $bmp.Dispose()
  $inkRatio = [math]::Round($ink / $sampled, 4)
  $name = Split-Path $t.Path -Leaf
  "$name  ${w}x${h}"
  "   ink ratio      : $inkRatio   (any-ink bbox $minX,$minY .. $maxX,$maxY)"
  "   content bbox   : $cMinX,$cMinY .. $cMaxX,$cMaxY   (margins L=$cMinX T=$cMinY R=$($w-$cMaxX) B=$($h-$cMaxY))"
  "   accent pixels  : $accent"
  "   edge-clipped   : $edgeInk"
  if ($inkRatio -lt $t.MinInk) { "   FAIL ink ratio below $($t.MinInk) -> render looks blank"; $fails++ }
  if ($t.ExpectAccent -and $accent -lt 200) { "   FAIL accent color missing"; $fails++ }
  if ($edgeInk -gt 0 -and -not $t.Tight) { "   FAIL real content is clipped at the canvas edge"; $fails++ }
  if ($t.Tight) { "   (tight-cropped logo: edge contact is intentional)" }
  # the full-width 4px brand bar sits at y=0..3 by design, so only judge the
  # left margin once we are below it
  $sideMinX = if ($cMinY -gt 10) { $cMinX } else { $w }
  if ($sideMinX -lt 40 -or ($w - $cMaxX) -lt 40 -or ($h - $cMaxY) -lt 20) {
    "   WARN real content margin looks thin"
  }
  ""
}
"FAILURES: $fails"
if ($fails -gt 0) { exit 1 }
