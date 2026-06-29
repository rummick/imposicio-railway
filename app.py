from flask import Flask, request, jsonify, send_file
import pikepdf
from pikepdf import Pdf, Rectangle
import fitz
from PIL import Image
import io, os, traceback

VERSION = '2.1'
app = Flask(__name__)
MM = 72 / 25.4
def pt(mm): return mm * MM

def copy_resources(resources, out):
    """Copy page resources into target PDF, handling direct and indirect objects."""
    res_out = pikepdf.Dictionary()
    for rkey in ['/Font','/XObject','/ExtGState','/ColorSpace',
                 '/Pattern','/Shading','/ProcSet','/Properties']:
        if rkey not in resources: continue
        v = resources[rkey]
        if v.is_indirect:
            try: res_out[rkey] = out.copy_foreign(v); continue
            except Exception: pass
        if isinstance(v, pikepdf.Dictionary):
            sub = pikepdf.Dictionary()
            for kk,vv in v.items():
                try: sub[kk] = out.copy_foreign(vv)
                except Exception: sub[kk] = vv
            res_out[rkey] = sub
        elif isinstance(v, pikepdf.Array):
            items = []
            for i in v:
                try: items.append(out.copy_foreign(i))
                except Exception: items.append(i)
            res_out[rkey] = pikepdf.Array(items)
        else:
            res_out[rkey] = v
    return res_out

def build_page_xobject(src_pdf, page_idx, out):
    """Build Form XObject for full page. Crop applied externally via clip+translate."""
    pg = src_pdf.pages[page_idx]
    mb = pg.mediabox
    x0,y0 = float(mb[0]),float(mb[1])
    pw = float(mb[2])-x0; ph = float(mb[3])-y0

    contents = pg.obj.get('/Contents')
    if isinstance(contents, pikepdf.Array):
        sd = b' '.join(s.read_bytes() for s in contents)
    else:
        sd = contents.read_bytes() if contents else b''

    # Normalize origin to 0,0 if needed
    if abs(x0) > 0.01 or abs(y0) > 0.01:
        sd = f"q {-x0:.4f} {-y0:.4f} cm\n".encode() + sd + b"\nQ"

    resources = pg.obj.get('/Resources', pikepdf.Dictionary())
    xobj = pikepdf.Stream(out, sd,
        Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form, FormType=1,
        BBox=pikepdf.Array([0, 0, pw, ph]),
        Resources=copy_resources(resources, out))
    return xobj, pw, ph

def extend_bleed(img, bpx, method):
    """Extend image borders by bpx pixels using stretch or mirror method."""
    w, h = img.size
    nw, nh = w+2*bpx, h+2*bpx
    out = Image.new(img.mode, (nw, nh))
    if method == 'stretch':
        out.paste(img.crop((0,0,w,1)).resize((w,bpx),Image.NEAREST),(bpx,0))
        out.paste(img.crop((0,h-1,w,h)).resize((w,bpx),Image.NEAREST),(bpx,nh-bpx))
        out.paste(img.crop((0,0,1,h)).resize((bpx,h),Image.NEAREST),(0,bpx))
        out.paste(img.crop((w-1,0,w,h)).resize((bpx,h),Image.NEAREST),(nw-bpx,bpx))
        out.paste(img.crop((0,0,1,1)).resize((bpx,bpx),Image.NEAREST),(0,0))
        out.paste(img.crop((w-1,0,w,1)).resize((bpx,bpx),Image.NEAREST),(nw-bpx,0))
        out.paste(img.crop((0,h-1,1,h)).resize((bpx,bpx),Image.NEAREST),(0,nh-bpx))
        out.paste(img.crop((w-1,h-1,w,h)).resize((bpx,bpx),Image.NEAREST),(nw-bpx,nh-bpx))
    else:  # mirror
        out.paste(img.crop((0,0,w,bpx)).transpose(Image.FLIP_TOP_BOTTOM),(bpx,0))
        out.paste(img.crop((0,h-bpx,w,h)).transpose(Image.FLIP_TOP_BOTTOM),(bpx,nh-bpx))
        out.paste(img.crop((0,0,bpx,h)).transpose(Image.FLIP_LEFT_RIGHT),(0,bpx))
        out.paste(img.crop((w-bpx,0,w,h)).transpose(Image.FLIP_LEFT_RIGHT),(nw-bpx,bpx))
        out.paste(img.crop((0,0,bpx,bpx)),(0,0))
        out.paste(img.crop((w-bpx,0,w,bpx)),(nw-bpx,0))
        out.paste(img.crop((0,h-bpx,bpx,h)),(0,nh-bpx))
        out.paste(img.crop((w-bpx,h-bpx,w,h)),(nw-bpx,nh-bpx))
    out.paste(img,(bpx,bpx))
    return out

COLORS = {'black':(0,0,0),'white':(1,1,1),'cyan':(0,1,1),
          'magenta':(1,0,1),'yellow':(1,1,0),'red':(1,0,0)}

def impose_vector(src_bytes, sw, sh, cw, ch, cols, rows, gap, sides, border, bw):
    src_pdf = Pdf.open(io.BytesIO(src_bytes))
    CW,CH = pt(cw),pt(ch)
    SW,SH,GP = pt(sw),pt(sh),pt(gap)
    sx_grid = (SW - cols*CW - (cols-1)*GP) / 2
    sy_grid = (SH - rows*CH - (rows-1)*GP) / 2
    num = min(sides, len(src_pdf.pages))
    out = Pdf.new()

    # Preserve optional content layers (InDesign/Illustrator)
    if '/OCProperties' in src_pdf.Root:
        try: out.Root['/OCProperties'] = out.copy_foreign(src_pdf.Root['/OCProperties'])
        except Exception: pass

    for pi in range(num):
        xobj, pw, ph = build_page_xobject(src_pdf, pi, out)
        crop_ox = (pw - CW) / 2
        crop_oy = (ph - CH) / 2

        xd = pikepdf.Dictionary(); xd["/C"] = xobj
        mirror = (pi == 1); lines = []

        for r in range(rows):
            for c in range(cols):
                ci = (cols-1-c) if mirror else c
                px = sx_grid + ci*(CW+GP)
                py = sy_grid + (rows-1-r)*(CH+GP)
                # Clip to card area, then translate so crop origin maps to card origin
                tx = px - crop_ox
                ty = py - crop_oy
                lines.append(
                    f"q {px:.3f} {py:.3f} {CW:.3f} {CH:.3f} re W n "
                    f"1 0 0 1 {tx:.3f} {ty:.3f} cm /C Do Q"
                )
                if border and bw > 0:
                    bc = border
                    lines.append(
                        f"{bc[0]} {bc[1]} {bc[2]} RG {pt(bw):.3f} w "
                        f"{px:.3f} {py:.3f} {CW:.3f} {CH:.3f} re S"
                    )

        out.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name.Page, MediaBox=[0,0,SW,SH],
            Resources=pikepdf.Dictionary(XObject=xd),
            Contents=pikepdf.Stream(out, "\n".join(lines).encode())
        )))

    buf = io.BytesIO(); out.save(buf); return buf.getvalue()

def impose_raster(src_bytes, sw, sh, cw, ch, bleed, cols, rows, gap, sides,
                  border, bw, dpi, method):
    doc = fitz.open(stream=src_bytes, filetype="pdf")
    out = Pdf.new()
    SW,SH = pt(sw),pt(sh)
    TW,TH = pt(cw+2*bleed), pt(ch+2*bleed)
    GP = pt(gap)
    sx = (SW - cols*TW - (cols-1)*GP) / 2
    sy = (SH - rows*TH - (rows-1)*GP) / 2
    scale = dpi / 72.0
    bpx = int(bleed * (dpi/25.4))  # bleed in pixels at target dpi

    for pi in range(min(sides, doc.page_count)):
        page = doc[pi]
        ow, oh = page.rect.width, page.rect.height
        cwpt, chpt = pt(cw), pt(ch)

        # Render page at target DPI
        pix = page.get_pixmap(matrix=fitz.Matrix(scale,scale), alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        if method == 'included':
            # Bleed already in PDF: crop to final+bleed size directly
            total_w = cwpt + 2*pt(bleed)
            total_h = chpt + 2*pt(bleed)
            ox0 = (ow - total_w) / 2; oy0 = (oh - total_h) / 2
            cx0 = max(0, int(ox0*scale)); cy0 = max(0, int(oy0*scale))
            cx1 = min(img.width, cx0 + int(total_w*scale))
            cy1 = min(img.height, cy0 + int(total_h*scale))
            wb = img.crop((cx0, cy0, cx1, cy1))
        else:
            # Generate bleed: crop to final size then extend borders
            ox0 = (ow - cwpt) / 2; oy0 = (oh - chpt) / 2
            cx0 = max(0, int(ox0*scale)); cy0 = max(0, int(oy0*scale))
            cx1 = min(img.width, cx0 + int(cwpt*scale))
            cy1 = min(img.height, cy0 + int(chpt*scale))
            wb = extend_bleed(img.crop((cx0, cy0, cx1, cy1)), bpx, method)

        # Store as JPEG (DCTDecode) — compact and correctly handled by all PDF viewers
        jpg_buf = io.BytesIO()
        wb.save(jpg_buf, 'JPEG', quality=92)
        jpg_data = jpg_buf.getvalue()

        im_stream = pikepdf.Stream(out, jpg_data,
            Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image,
            Width=wb.width, Height=wb.height,
            ColorSpace=pikepdf.Name.DeviceRGB,
            BitsPerComponent=8,
            Filter=pikepdf.Name.DCTDecode)

        imd = pikepdf.Dictionary()
        imd[pikepdf.Name.Im] = im_stream
        mirror = (pi == 1); lines = []
        for r in range(rows):
            for c in range(cols):
                ci = (cols-1-c) if mirror else c
                x = sx + ci*(TW+GP); y = sy + (rows-1-r)*(TH+GP)
                # Scale+translate image to card position
                lines.append(f"q {TW:.3f} 0 0 {TH:.3f} {x:.3f} {y:.3f} cm /Im Do Q")
                if border and bw > 0:
                    bc = border
                    lines.append(
                        f"{bc[0]} {bc[1]} {bc[2]} RG {pt(bw):.3f} w "
                        f"{x:.3f} {y:.3f} {TW:.3f} {TH:.3f} re S"
                    )

        out.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name.Page, MediaBox=[0,0,SW,SH],
            Resources=pikepdf.Dictionary(XObject=imd),
            Contents=pikepdf.Stream(out, "\n".join(lines).encode())
        )))

    buf = io.BytesIO(); out.save(buf); return buf.getvalue()


@app.after_request
def cors(r):
    r.headers['Access-Control-Allow-Origin'] = '*'
    r.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return r

@app.route('/api/impose', methods=['OPTIONS'])
def options(): return '', 200

@app.route('/api/info', methods=['POST'])
def info():
    try:
        pdf_bytes = request.files['pdf'].read()
        src = Pdf.open(io.BytesIO(pdf_bytes))
        pg = src.pages[0]; mb = pg.mediabox
        return jsonify({
            'pageW': round((float(mb[2])-float(mb[0]))/MM, 2),
            'pageH': round((float(mb[3])-float(mb[1]))/MM, 2),
            'pages': len(src.pages)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/impose', methods=['POST'])
def impose():
    try:
        pdf_bytes = request.files['pdf'].read()
        p = request.form
        sw    = float(p.get('sheetW', 320))
        sh    = float(p.get('sheetH', 450))
        cw    = float(p.get('cropW', 0)) or None
        ch    = float(p.get('cropH', 0)) or None
        cols  = int(p.get('cols', 3))
        rows  = int(p.get('rows', 6))
        gap   = float(p.get('gap', 0))
        sides = int(p.get('sides', 1))
        bleed = float(p.get('bleedMM', 0))
        bmet  = p.get('bleedMethod', 'stretch')
        dpi   = int(p.get('renderDPI', 300))
        bcol  = COLORS.get(p.get('borderColor', ''))
        bw    = float(p.get('borderWidth', 0.25))

        src = Pdf.open(io.BytesIO(pdf_bytes))
        pg0 = src.pages[0]; mb = pg0.mediabox
        cw = cw or round((float(mb[2])-float(mb[0]))/MM, 2)
        ch = ch or round((float(mb[3])-float(mb[1]))/MM, 2)

        if bleed > 0:
            result = impose_raster(pdf_bytes, sw, sh, cw, ch, bleed,
                                   cols, rows, gap, sides, bcol, bw, dpi, bmet)
        else:
            result = impose_vector(pdf_bytes, sw, sh, cw, ch,
                                   cols, rows, gap, sides, bcol, bw)

        return send_file(io.BytesIO(result), mimetype='application/pdf',
                         as_attachment=True,
                         download_name=f'impose_{cols}x{rows}.pdf')
    except Exception as e:
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500

@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'imposicio-railway', 'version': VERSION})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
