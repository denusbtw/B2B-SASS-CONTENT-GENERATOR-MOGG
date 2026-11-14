"""Render a declarative cartoon plan using trusted drawing and FFmpeg filters."""

from pathlib import Path
import wave

from PIL import Image, ImageDraw

from factory import media
from factory.contracts import CartoonScene, Cue, EpisodeScript, Mode, Timeline
from factory.director import NeedsReview
from factory.openai_director import speech


def scene_art(scene: CartoonScene, path: Path) -> None:
    colors = {
        "room": ("#232c44", "#4a3543"),
        "forest": ("#253749", "#101e26"),
        "tower": ("#413551", "#20283e"),
        "road": ("#393349", "#222936"),
        "phone": ("#29394c", "#141c2d"),
    }
    sky, ground = colors[scene.setting]
    canvas = Image.new("RGB", (1080, 1920), sky)
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((700, 185, 875, 360), fill="#efc997")
    draw.polygon(
        [
            (0, 980),
            (270, 655),
            (525, 860),
            (780, 585),
            (1080, 880),
            (1080, 1920),
            (0, 1920),
        ],
        fill=ground,
    )
    if scene.setting in {"forest", "road", "tower"}:
        for x in range(-80, 1140, 145):
            top = 780 + (x * 37) % 220
            draw.polygon([(x, top), (x - 130, 1440), (x + 130, 1440)], fill="#172734")
    if scene.setting == "tower":
        draw.line(
            [(420, 1310), (510, 530), (685, 530), (760, 1310)], fill="#827382", width=25
        )
        draw.rectangle((410, 390, 765, 640), fill="#343042")
        draw.rectangle((460, 450, 715, 565), fill="#e6bb79")
    if scene.setting == "road":
        draw.polygon([(510, 900), (625, 900), (1040, 1920), (90, 1920)], fill="#505468")
    if scene.setting == "room":
        draw.rectangle((180, 350, 900, 1010), fill="#171e2c")
        draw.rectangle((210, 380, 870, 980), fill="#42465f")
        draw.line((540, 380, 540, 980), fill="#212738", width=22)
    if scene.setting == "phone":
        draw.rounded_rectangle(
            (270, 385, 810, 1360), radius=64, fill="#141928", outline="#8589a4", width=9
        )
        draw.rounded_rectangle((315, 470, 765, 1240), radius=44, fill="#35485b")
        draw.rounded_rectangle((350, 610, 730, 790), radius=25, fill="#e4c692")
    else:
        draw.rounded_rectangle((390, 1020, 665, 1490), radius=75, fill="#435c69")
        draw.ellipse((412, 830, 641, 1080), fill="#d4a888")
        draw.pieslice((400, 807, 653, 1040), 180, 360, fill="#232634")
        for x in (472, 568):
            draw.ellipse((x, 950, x + 14, 973), fill="#1a2029")
        if scene.action in {"shake", "fall", "signal"}:
            draw.ellipse((512, 995, 550, 1028), fill="#593b44")
            draw.line((634, 1110, 745, 930), fill="#435c69", width=64)
    if scene.overlay:
        import textwrap

        label = "\n".join(textwrap.wrap(scene.overlay, width=16))
        draw.multiline_text(
            (540, 690 if scene.setting == "phone" else 540),
            label,
            font=media.font(29),
            fill="#201c27" if scene.setting == "phone" else "#ffe4b1",
            anchor="mm",
            align="center",
        )
    if scene.action == "fall":
        canvas = canvas.rotate(
            -12, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=sky
        )
    canvas.save(path)


def render_episode(
    episode: EpisodeScript, part: int, voice: str, work: Path, target: float
) -> tuple[Path, str, Timeline]:
    cues = []
    films = []
    elapsed = 0
    for number, scene in enumerate(episode.scenes, 1):
        stem = f"scene-{number:02}"
        sound = work / (stem + ".wav")
        artwork = work / (stem + ".png")
        film = stem + ".mp4"
        sound.write_bytes(speech(scene, voice))
        try:
            with wave.open(str(sound), "rb") as audio:
                frames = max(1, round(audio.getnframes() / audio.getframerate() * 30))
        except (wave.Error, EOFError, ZeroDivisionError):
            raise NeedsReview("Invalid narration audio") from None
        if frames > 900:
            raise NeedsReview("A narration segment exceeds 30 seconds")
        scene_art(scene, artwork)
        zoom = {
            "push": "min(1.15,1+on*0.0007)",
            "pull": "max(1,1.15-on*0.0007)",
            "pan": "1.12",
            "still": "1",
        }[scene.camera]
        x = "iw/2-iw/zoom/2"
        if scene.camera == "pan" or scene.action == "walk":
            zoom = "1.13"
            x = f"(iw-iw/zoom)*on/{frames}"
        if scene.action == "shake":
            zoom = "1.13"
            x = "(iw-iw/zoom)*(0.5+0.12*sin(on*0.9))"
        media.run(
            [
                media.ffmpeg(),
                "-y",
                "-v",
                "error",
                "-i",
                artwork.name,
                "-i",
                sound.name,
                "-filter_threads",
                "1",
                "-vf",
                f"zoompan=z='{zoom}':x='{x}':y='ih/2-ih/zoom/2':d={frames}:s=1080x1920:fps=30",
                "-af",
                "apad",
                "-t",
                f"{frames / 30:.6f}",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "24000",
                "-ac",
                "1",
                film,
            ],
            work,
        )
        end = elapsed + frames
        cues.append(
            Cue(
                start_ms=round(elapsed * 1000 / 30),
                end_ms=round(end * 1000 / 30),
                text=scene.narration,
            )
        )
        elapsed = end
        films.append(f"file '{film}'")
    if abs(elapsed / 30 - target) > max(5, target * 0.15):
        raise NeedsReview("Narrated duration is outside tolerance")
    timeline = Timeline(
        mode=Mode.CARTOON_2D,
        part=part,
        cues=cues,
        duration_ms=cues[-1].end_ms,
        width=1080,
        height=1920,
        fps=30,
    )
    srt = media.subtitles(timeline)
    (work / "captions.srt").write_text(srt, encoding="utf-8")
    (work / "scenes.txt").write_text("\n".join(films) + "\n", encoding="ascii")
    output = work / "video.mp4"
    media.run(
        [
            media.ffmpeg(),
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            "scenes.txt",
            "-filter_threads",
            "1",
            "-vf",
            "subtitles=captions.srt:force_style='FontName=DejaVu Sans,FontSize=17,Alignment=2,MarginV=38,Outline=2'",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            output.name,
        ],
        work,
        timeout=420,
    )
    return output, srt, timeline
