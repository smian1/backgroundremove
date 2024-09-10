import os
import uuid
import warnings
import logging
from flask import Flask, render_template, request, send_file, jsonify
from werkzeug.utils import secure_filename
from PIL import Image
from transparent_background import Remover
import io
import base64
from PIL import ExifTags
from pillow_heif import register_heif_opener
import pillow_heif  # Add this line

# Register HEIF opener
register_heif_opener()

# Suppress FutureWarning
warnings.filterwarnings("ignore", category=FutureWarning)

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

try:
    remover = Remover()
except Exception as e:
    print(f"Error initializing Remover: {e}")
    remover = None

def rotate_image(image):
    try:
        for orientation in ExifTags.TAGS.keys():
            if ExifTags.TAGS[orientation] == 'Orientation':
                break
        exif = dict(image._getexif().items())

        if exif[orientation] == 3:
            image = image.rotate(180, expand=True)
        elif exif[orientation] == 6:
            image = image.rotate(270, expand=True)
        elif exif[orientation] == 8:
            image = image.rotate(90, expand=True)
    except (AttributeError, KeyError, IndexError):
        # Cases: image don't have getexif
        pass
    return image

# Add this dictionary to store processed images temporarily
processed_images = {}

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        logger.debug("POST request received")
        if 'file' not in request.files:
            logger.warning("No file part in the request")
            return jsonify({'error': 'No file part'})
        
        file = request.files['file']
        if file.filename == '':
            logger.warning("No selected file")
            return jsonify({'error': 'No selected file'})
        
        if file and remover:
            try:
                logger.debug(f"Processing file: {file.filename}")
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                logger.debug(f"File saved to {filepath}")
                
                # Check if the file is HEIC
                if filename.lower().endswith('.heic'):
                    img = Image.open(filepath)
                    # Convert HEIC to PNG in memory
                    with io.BytesIO() as f:
                        img.save(f, format='PNG')
                        f.seek(0)
                        img = Image.open(f)
                else:
                    img = Image.open(filepath)
                
                img = rotate_image(img)  # Apply rotation if needed
                img = img.convert('RGB')
                logger.debug("Image opened, rotated if needed, and converted to RGB")
                out = remover.process(img)
                logger.debug("Background removed from image")
                
                # Generate a unique ID for the processed image
                image_id = str(uuid.uuid4())
                
                # Store the processed image in memory
                processed_images[image_id] = out
                
                # Convert to base64 for displaying in HTML
                img_buffer = io.BytesIO()
                out.save(img_buffer, format='PNG')
                img_buffer.seek(0)
                img_base64 = base64.b64encode(img_buffer.getvalue()).decode('ascii')
                
                os.remove(filepath)
                logger.debug(f"Temporary file {filepath} removed")
                
                return jsonify({'image': img_base64, 'id': image_id})
            except Exception as e:
                logger.error(f"Error processing image: {str(e)}", exc_info=True)
                return jsonify({'error': f'Error processing image: {str(e)}'})
        else:
            logger.error("Background removal is currently unavailable")
            return jsonify({'error': 'Background removal is currently unavailable'})
    
    return render_template('index.html')

@app.route('/download/<image_id>/<format>', methods=['GET'])
def download(image_id, format):
    if image_id not in processed_images:
        logger.error(f"Image not found: {image_id}")
        return jsonify({'error': 'Image not found'}), 404
    
    img = processed_images[image_id]
    format = format.upper()
    
    logger.debug(f"Preparing to save image as {format}. Image mode: {img.mode}")
    
    img_io = io.BytesIO()
    
    try:
        if format == 'PNG':
            img.save(img_io, format='PNG')
        elif format == 'JPEG':
            # Handle transparency for JPEG
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3])  # 3 is the alpha channel
                img = background
            
            img = img.convert('RGB')
            logger.debug(f"Converted image to RGB for JPEG. New mode: {img.mode}")
            img.save(img_io, format='JPEG', quality=95)
        elif format == 'WEBP':
            img.save(img_io, format='WEBP', quality=95)
        else:
            logger.error(f"Unsupported format: {format}")
            return jsonify({'error': 'Unsupported format'}), 400
        
        img_io.seek(0)
        logger.debug(f"Image saved as {format}. Size: {img_io.getbuffer().nbytes} bytes")
        
        return send_file(
            img_io, 
            mimetype=f'image/{format.lower()}', 
            as_attachment=True, 
            download_name=f'background_removed_{image_id[:8]}.{format.lower()}'
        )
    except Exception as e:
        logger.error(f"Error saving image as {format}: {str(e)}", exc_info=True)
        return jsonify({'error': f'Error saving image as {format}'}), 500

# Add a new route to clear the processed image when starting a new removal process
@app.route('/clear/<image_id>', methods=['POST'])
def clear_processed_image(image_id):
    if image_id in processed_images:
        del processed_images[image_id]
        return jsonify({'message': 'Processed image cleared'}), 200
    return jsonify({'message': 'Image not found'}), 404

@app.route('/debug/<image_id>', methods=['GET'])
def debug_image(image_id):
    if image_id not in processed_images:
        return jsonify({'error': 'Image not found'}), 404
    
    img = processed_images[image_id]
    return jsonify({
        'mode': img.mode,
        'size': img.size,
        'format': img.format,
        'has_transparency': img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
    })

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'})
    
    if file:
        try:
            # Generate a unique filename for pasted images
            if file.filename == 'blob':
                file.filename = f"pasted_image_{uuid.uuid4()}.png"

            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            # Convert HEIC to PNG if necessary
            if filename.lower().endswith('.heic'):
                heif_file = pillow_heif.read_heif(filepath)
                image = Image.frombytes(
                    heif_file.mode, 
                    heif_file.size, 
                    heif_file.data,
                    "raw",
                    heif_file.mode,
                    heif_file.stride,
                )
            else:
                image = Image.open(filepath)
            
            # Rotate image if needed
            image = rotate_image(image)
            
            # Convert to RGB
            image = image.convert('RGB')
            
            # Save as PNG
            png_filepath = os.path.splitext(filepath)[0] + '.png'
            image.save(png_filepath, 'PNG')
            
            # Convert to base64 for preview
            with open(png_filepath, 'rb') as img_file:
                img_base64 = base64.b64encode(img_file.read()).decode('ascii')
            
            # Clean up original file if it was HEIC
            if filename.lower().endswith('.heic'):
                os.remove(filepath)
            
            return jsonify({'preview': img_base64, 'filepath': png_filepath})
        except Exception as e:
            return jsonify({'error': f'Error processing image: {str(e)}'})
    
    return jsonify({'error': 'Invalid file'})

@app.route('/process', methods=['POST'])
def process():
    filepath = request.json.get('filepath')
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'Invalid file path'})
    
    try:
        img = Image.open(filepath)
        out = remover.process(img)
        
        # Generate a unique ID for the processed image
        image_id = str(uuid.uuid4())
        
        # Store the processed image in memory
        processed_images[image_id] = out
        
        # Convert to base64 for displaying in HTML
        img_buffer = io.BytesIO()
        out.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        img_base64 = base64.b64encode(img_buffer.getvalue()).decode('ascii')
        
        # Clean up the temporary PNG file
        os.remove(filepath)
        
        return jsonify({'image': img_base64, 'id': image_id})
    except Exception as e:
        logger.error(f"Error processing image: {str(e)}", exc_info=True)
        return jsonify({'error': f'Error processing image: {str(e)}'})

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
<<<<<<< HEAD
    app.run(debug=True, host='0.0.0.0', port=5000)
=======
    app.run(host='0.0.0.0', port=8080, debug=True)
>>>>>>> c443152d9574b52dcf02d4094b813274dbc16359
