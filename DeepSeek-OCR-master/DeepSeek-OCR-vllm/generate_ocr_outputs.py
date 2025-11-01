import os
import pickle
import argparse
import re
import io
from tqdm import tqdm
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import torch
from config import MODEL_PATH, OUTPUT_PATH, PROMPT, SKIP_REPEAT, MAX_CONCURRENCY, CROP_MODE
from deepseek_ocr import DeepseekOCRForCausalLM
from vllm.model_executor.models.registry import ModelRegistry
from vllm import LLM, SamplingParams
from process.ngram_norepeat import NoRepeatNGramLogitsProcessor


# Set environment variables
if torch.version.cuda == '11.8':
    os.environ["TRITON_PTXAS_PATH"] = "/usr/local/cuda-11.8/bin/ptxas"
os.environ['VLLM_USE_V1'] = '0'
os.environ["CUDA_VISIBLE_DEVICES"] = '0'


def load_tokenized_batch(batch_file):
    """Load a batch of tokenized data"""
    with open(batch_file, 'rb') as f:
        data = pickle.load(f)
    return data


def load_all_tokenized_data(tokenized_dir):
    """Load all tokenized data from directory"""
    batch_files = [os.path.join(tokenized_dir, f) for f in os.listdir(tokenized_dir)
                   if f.startswith('tokenized_batch_') and f.endswith('.pkl')]
    batch_files.sort()

    all_tokenized_data = []
    all_metadata = []

    for batch_file in batch_files:
        print(f"Loading {batch_file}")
        batch_data = load_tokenized_batch(batch_file)
        all_tokenized_data.extend(batch_data['tokenized_data'])
        all_metadata.extend(batch_data['metadata'])

    return all_tokenized_data, all_metadata


def load_single_tokenized_pdf(tokenized_file):
    """Load tokenized data for a single PDF"""
    with open(tokenized_file, 'rb') as f:
        data = pickle.load(f)
    return data['tokenized_data'], data


def re_match(text):
    pattern = r'(<\|ref\|>(.*?)<\|/ref\|><\|det\|>(.*?)<\|/det\|>)'
    matches = re.findall(pattern, text, re.DOTALL)

    mathes_image = []
    mathes_other = []
    for a_match in matches:
        if '<|ref|>image<|/ref|>' in a_match[0]:
            mathes_image.append(a_match[0])
        else:
            mathes_other.append(a_match[0])
    return matches, mathes_image, mathes_other


def extract_coordinates_and_label(ref_text, image_width, image_height):
    try:
        label_type = ref_text[1]
        cor_list = eval(ref_text[2])
    except Exception as e:
        print(e)
        return None
    return (label_type, cor_list)


def draw_bounding_boxes(image, refs, jdx, output_path):
    image_width, image_height = image.size
    img_draw = image.copy()
    draw = ImageDraw.Draw(img_draw)

    overlay = Image.new('RGBA', img_draw.size, (0, 0, 0, 0))
    draw2 = ImageDraw.Draw(overlay)

    font = ImageFont.load_default()

    img_idx = 0

    for i, ref in enumerate(refs):
        try:
            result = extract_coordinates_and_label(ref, image_width, image_height)
            if result:
                label_type, points_list = result

                color = (np.random.randint(0, 200), np.random.randint(0, 200), np.random.randint(0, 255))
                color_a = color + (20, )

                for points in points_list:
                    x1, y1, x2, y2 = points

                    x1 = int(x1 / 999 * image_width)
                    y1 = int(y1 / 999 * image_height)
                    x2 = int(x2 / 999 * image_width)
                    y2 = int(y2 / 999 * image_height)

                    if label_type == 'image':
                        try:
                            cropped = image.crop((x1, y1, x2, y2))
                            cropped.save(f"{output_path}/images/{jdx}_{img_idx}.jpg")
                        except Exception as e:
                            print(e)
                            pass
                        img_idx += 1

                    try:
                        if label_type == 'title':
                            draw.rectangle([x1, y1, x2, y2], outline=color, width=4)
                            draw2.rectangle([x1, y1, x2, y2], fill=color_a, outline=(0, 0, 0, 0), width=1)
                        else:
                            draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
                            draw2.rectangle([x1, y1, x2, y2], fill=color_a, outline=(0, 0, 0, 0), width=1)

                        text_x = x1
                        text_y = max(0, y1 - 15)

                        text_bbox = draw.textbbox((0, 0), label_type, font=font)
                        text_width = text_bbox[2] - text_bbox[0]
                        text_height = text_bbox[3] - text_bbox[1]
                        draw.rectangle([text_x, text_y, text_x + text_width, text_y + text_height],
                                    fill=(255, 255, 255, 30))

                        draw.text((text_x, text_y), label_type, font=font, fill=color)
                    except:
                        pass
        except:
            continue
    img_draw.paste(overlay, (0, 0), overlay)
    return img_draw


def process_image_with_refs(image, ref_texts, jdx, output_path):
    result_image = draw_bounding_boxes(image, ref_texts, jdx, output_path)
    return result_image


def pil_to_pdf_img2pdf(pil_images, output_path):
    import img2pdf

    if not pil_images:
        return

    image_bytes_list = []

    for img in pil_images:
        if img.mode != 'RGB':
            img = img.convert('RGB')

        img_buffer = io.BytesIO()
        img.save(img_buffer, format='JPEG', quality=95)
        img_bytes = img_buffer.getvalue()
        image_bytes_list.append(img_bytes)

    try:
        pdf_bytes = img2pdf.convert(image_bytes_list)
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
    except Exception as e:
        print(f"error: {e}")


def generate_ocr_outputs(tokenized_data, metadata, original_images, output_path, llm, sampling_params):
    """Generate OCR outputs from tokenized data"""

    os.makedirs(output_path, exist_ok=True)
    os.makedirs(f'{output_path}/images', exist_ok=True)

    # Generate outputs in batches
    outputs_list = llm.generate(tokenized_data, sampling_params=sampling_params)

    # Process results
    mmd_det_path = os.path.join(output_path, 'combined_det.mmd')
    mmd_path = os.path.join(output_path, 'combined.mmd')
    pdf_out_path = os.path.join(output_path, 'combined_layouts.pdf')

    contents_det = ''
    contents = ''
    draw_images = []
    jdx = 0

    for output, img in zip(outputs_list, original_images):
        content = output.outputs[0].text

        if '<｜end▁of▁sentence｜>' in content:
            content = content.replace('<｜end▁of▁sentence｜>', '')
        else:
            if SKIP_REPEAT:
                continue

        page_num = f'\n<--- Page Split --->'
        contents_det += content + f'\n{page_num}\n'

        image_draw = img.copy()
        matches_ref, matches_images, mathes_other = re_match(content)
        result_image = process_image_with_refs(image_draw, matches_ref, jdx, output_path)

        draw_images.append(result_image)

        for idx, a_match_image in enumerate(matches_images):
            content = content.replace(a_match_image, f'![](images/' + str(jdx) + '_' + str(idx) + '.jpg)\n')

        for idx, a_match_other in enumerate(mathes_other):
            content = content.replace(a_match_other, '').replace('\\coloneqq', ':=').replace('\\eqqcolon', '=:').replace('\n\n\n\n', '\n\n').replace('\n\n\n', '\n\n')

        contents += content + f'\n{page_num}\n'
        jdx += 1

    # Save outputs
    with open(mmd_det_path, 'w', encoding='utf-8') as afile:
        afile.write(contents_det)

    with open(mmd_path, 'w', encoding='utf-8') as afile:
        afile.write(contents)

    pil_to_pdf_img2pdf(draw_images, pdf_out_path)

    print(f"Generated outputs saved to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate OCR outputs from tokenized data")
    parser.add_argument("--tokenized-dir", "-t", help="Directory containing tokenized data batches")
    parser.add_argument("--tokenized-file", "-f", help="Single tokenized data file")
    parser.add_argument("--original-pdfs", "-p", help="Directory containing original PDF files")
    parser.add_argument("--output", "-o", required=True, help="Output directory")
    parser.add_argument("--batch-size", "-b", type=int, default=100, help="Batch size for VLLM generation")

    args = parser.parse_args()

    # Initialize VLLM model
    ModelRegistry.register_model("DeepseekOCRForCausalLM", DeepseekOCRForCausalLM)

    llm = LLM(
        model=MODEL_PATH,
        hf_overrides={"architectures": ["DeepseekOCRForCausalLM"]},
        block_size=256,
        enforce_eager=False,
        trust_remote_code=True,
        max_model_len=8192,
        swap_space=0,
        max_num_seqs=MAX_CONCURRENCY,
        tensor_parallel_size=1,
        gpu_memory_utilization=0.9,
        disable_mm_preprocessor_cache=True,
        dtype='bfloat16'
    )

    logits_processors = [NoRepeatNGramLogitsProcessor(ngram_size=20, window_size=50, whitelist_token_ids={128821, 128822})]
    sampling_params = SamplingParams(
        temperature=0.0,
        max_tokens=8192,
        logits_processors=logits_processors,
        skip_special_tokens=False,
        include_stop_str_in_output=True,
    )

    # Load tokenized data
    if args.tokenized_file:
        tokenized_data, metadata = load_single_tokenized_pdf(args.tokenized_file)
        # Need to reconstruct original images from PDFs
        import fitz
        pdf_path = metadata['pdf_path']
        images = []
        pdf_document = fitz.open(pdf_path)
        zoom = 144 / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for page_num in range(pdf_document.page_count):
            page = pdf_document[page_num]
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            img_data = pixmap.tobytes("png")
            img = Image.open(io.BytesIO(img_data))
            images.append(img)
        pdf_document.close()
        original_images = images
    else:
        tokenized_data, metadata = load_all_tokenized_data(args.tokenized_dir)
        # This would require mapping back to original images - for now assume single batch
        # In practice, you'd need to store original images or reconstruct them
        print("Warning: Multi-batch processing requires original image reconstruction")
        return

    # Process in batches for VLLM
    for i in range(0, len(tokenized_data), args.batch_size):
        batch_end = min(i + args.batch_size, len(tokenized_data))
        batch_tokenized = tokenized_data[i:batch_end]
        batch_images = original_images[i:batch_end]

        print(f"Processing batch {i//args.batch_size + 1}: {len(batch_tokenized)} items")

        batch_output_dir = os.path.join(args.output, f"batch_{i//args.batch_size + 1:04d}")
        generate_ocr_outputs(batch_tokenized, metadata, batch_images, batch_output_dir, llm, sampling_params)


if __name__ == "__main__":
    main()