# Generating mood visuals on an RX 9060 XT / Windows 11

Research only — **nothing here is installed.** Researched 2026-08-17; this field moves
fast enough that anything older than a few months is worth re-checking before acting.

Target use: **offline generation of ambient loops for four fixed moods**, played back
live. Generation speed is nearly irrelevant — a loop can take an hour. Reliability, model
choice and visual consistency are what matter.

## Verdict

**Use ComfyUI Desktop with official AMD ROCm.** It stopped being a hack in January 2026.

The headline finding is a piece of luck: **Windows ROCm officially supports only
`gfx1200`/`gfx1201` — the RDNA 4 chips.** The RX 9060 XT is RDNA 4. The previous
generation (RX 7000 / RDNA 3) has *no* Windows ROCm at all, so most of the "AMD on
Windows is painful" advice online predates this and no longer describes your hardware.
You are on the first AMD generation where this is a supported path rather than a
workaround.

## The options, ranked for this use case

| Path | State | Verdict |
|---|---|---|
| **ComfyUI Desktop + ROCm** | Official since v0.7.0 (Jan 2026), built on ROCm 7.1.1 | **Recommended** |
| **Amuse 3.0** | AMD/TensorStack app, ROCm-accelerated, RDNA3+ | Good fallback, far less control |
| ComfyUI-ZLUDA | Actively maintained (updates into 2026) | Superseded; only if ROCm fails |
| DirectML | Works on any DX12 GPU | Slowest; last resort |

**ComfyUI Desktop + ROCm** is the recommendation because your requirement is not "make an
image" but "make four visually consistent worlds and regenerate them as the show evolves".
That needs reproducible workflows, seeds, LoRAs and img2img chains — ComfyUI's whole
model. There is a GUI installer that handles dependencies, and Desktop, Portable and Git
installs are all supported. AMD claims up to 5.4× uplift over the previous Windows story.

**Amuse 3.0** is the zero-effort alternative: 100+ optimised models including SD 3.5 and
FLUX, images plus draft-quality video to ~6 s, ROCm-accelerated on Windows. Reach for it
if ComfyUI turns fiddly — but it is an appliance, and four coherent mood identities are
exactly the job that wants control.

⚠️ **ZLUDA is a CUDA translation layer, not an AMD path.** It worked well and is still
maintained, but it now adds a translation layer to solve a problem that has an official
answer. Only worth it if ROCm misbehaves on this specific card.

## VRAM — 16 GB is comfortable here

| Model | Approx VRAM | Note |
|---|---|---|
| SD 1.5 | ~4 GB | Huge LoRA/ControlNet ecosystem |
| SD 3.5 Medium | ~8–10 GB | Good quality/size balance |
| SDXL | ~10–12 GB | Still the workhorse |
| FLUX.1 dev (fp8) | ~12–16 GB | Tight; best quality |
| AnimateDiff (SD 1.5) | ~8–12 GB | The realistic route to **loops** |

**Because generation is offline, pick for quality and ignore speed.** Do not reach for
turbo/distilled models — those exist to trade quality for latency you do not care about.
Waiting two minutes a frame in the studio costs nothing.

⚠️ **You want loops, and most video models do not loop.** SVD-class models produce a short
clip with a beginning and an end; cutting that into a seamless loop is a separate problem.
**AnimateDiff has explicit looping support** and is the more direct route. The other honest
option is generating stills and doing the motion in post — slow pans, dissolves,
displacement — which for ambient texture often looks better than generated motion and is
completely predictable.

## What I could not verify

- **Whether the plain RX 9060 XT (non-LP) is on the ROCm 7.2 list.** One source names
  "RX 9060 XT LP" specifically; the 7.0.2 announcement says "RX 9060" generally. The
  architecture is right either way, but confirm your exact card before assuming.
- **Driver interaction.** ComfyUI's ROCm release recommends the **ROCm 7.1.1 Preview
  driver**. This machine runs Adrenalin `32.0.31035.1003`. Whether installing the preview
  driver disturbs the display driver is unknown to me, and this machine has a documented
  history of driver-related instability — check `devices/desktop.md` in the memory repo
  before touching it.
- **Real performance on this card.** No benchmarks found for the 9060 XT 16 GB. AMD's
  "optimal experience" spec is a Ryzen AI Max+ 128 GB or Radeon AI Pro R9700 with 64 GB —
  far above this box. That is a recommendation, not a requirement, but expect the card to
  be slower than the marketing figures.
- **FLUX at 16 GB without offloading.** Plausible at fp8, unconfirmed.

## Suggested order

1. Install **ComfyUI Desktop** and try it on the *existing* Adrenalin driver first. If it
   works, stop — you have avoided a driver change on a machine with a BSOD history.
2. Start with **SDXL or SD 3.5 Medium**. Prove one mood end to end before building four.
3. Settle the loop question early: **AnimateDiff** vs stills-plus-motion-in-post. It
   changes the whole workflow and is cheap to test both ways.
4. Only consider the ROCm preview driver if step 1 fails, and read the desktop's GPU
   history first.

## Sources

[ROCm 7.0.2 adds RX 9060 support](https://www.phoronix.com/news/AMD-ROCm-7.0.2-Released) ·
[ROCm 7.2 on Windows, RDNA 3 & 4 tested](https://runaihome.com/blog/amd-rocm-local-ai-2026/) ·
[Official AMD ROCm support in ComfyUI Desktop](https://blog.comfy.org/p/official-amd-rocm-support-arrives) ·
[Install ComfyUI on ROCm (AMD docs)](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/advanced/advancedrad/windows/comfyui/installcomfyui.html) ·
[AMD × ComfyUI](https://www.amd.com/en/blogs/2026/amd-comfyui-advancing-professional-quality-generative-ai-ryzen-radeon.html) ·
[Amuse 3.0](https://www.techpowerup.com/335487/amd-announces-amuse-3-0-generative-ai-solution-for-print-quality-images-and-draft-quality-short-videos) ·
[ComfyUI-Zluda](https://github.com/patientx/ComfyUI-Zluda) ·
[ROCm compatibility matrix](https://rocm.docs.amd.com/en/docs-7.1.0/compatibility/compatibility-matrix.html)
