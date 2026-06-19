from flask import Flask, request, jsonify, send_file
import pikepdf
from pikepdf import Pdf, Rectangle
import fitz  # PyMuPDF
from PIL import Image
import io, base64, os, traceback

app = Flask(__name__)

MM = 72 / 25.4

def pt(mm): return mm * MM

def extend_bleed(img, bpx, method):
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
    else:
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

COLORS = {
    'black':(0,0,0), 'white':(1,1,1), 'cyan':(0,1,1),
    'magenta':(1,0,1), 'yellow':(1,1,0), 'red':(1,0,0)
}

def impose_vector(src_bytes, sw, sh, cw, ch, cols, rows, gap, sides, border, bw):
    # Save/reload per garantir objectes indirectes
    buf0 = io.BytesIO()
    Pdf.open(io.BytesIO(src_bytes)).save(buf0)
    src_bytes2 = buf0.getvalue()

    SW,SH,CW,CH,GP = pt(sw),pt(sh),pt(cw),pt(ch),pt(gap)
    sx = (SW - cols*CW - (cols-1)*GP) / 2
    sy = (SH - rows*CH - (rows-1)*GP) / 2

    result_pages = []

    for pi in range(min(sides, Pdf.open(io.BytesIO(src_bytes2)).page_count)):
        src = Pdf.open(io.BytesIO(src_bytes2))
        pg = src.pages[pi]
        mb = pg.mediabox
        ox,oy = float(mb[0]),float(mb[1])
        ow,oh = float(mb[2])-ox, float(mb[3])-oy

        # Aplica cropbox
        cx0 = ox + (ow-CW)/2
        cy0 = oy + (oh-CH)/2
        pg.cropbox = Rectangle(cx0, cy0, cx0+CW, cy0+CH)
        pg.mediabox = Rectangle(cx0, cy0, cx0+CW, cy0+CH)

        # Guarda pàgina com PDF independent i recarrega
        single = Pdf.new()
        single.pages.append(single.copy_foreign(pg))
        sbuf = io.BytesIO(); single.save(sbuf); sbuf.seek(0)
        single2 = Pdf.open(sbuf)

        # Ara sí podem fer as_form_xobject
        out = Pdf.new()
        xobj = out.copy_foreign(single2.pages[0].as_form_xobject())
        xd = pikepdf.Dictionary(); xd["/C"] = xobj
        res = pikepdf.Dictionary(XObject=xd)

        mirror = (pi == 1)
        lines = []
        for r in range(rows):
            for c in range(cols):
                ci = (cols-1-c) if mirror else c
                x = sx + ci*(CW+GP)
                y = sy + (rows-1-r)*(CH+GP)
                lines.append(f"q 1 0 0 1 {x:.3f} {y:.3f} cm /C Do Q")
                if border and bw > 0:
                    bwpt = pt(bw); bc = border
                    lines.append(f"{bc[0]} {bc[1]} {bc[2]} RG {bwpt:.3f} w "
                                 f"{x:.3f} {y:.3f} {CW:.3f} {CH:.3f} re S")

        sheet = pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name.Page, MediaBox=[0,0,SW,SH],
            Resources=res,
            Contents=pikepdf.Stream(out, "\n".join(lines).encode())
        ))
        out.pages.append(sheet)
        page_buf = io.BytesIO(); out.save(page_buf)
        result_pages.append(page_buf.getvalue())

    # Combina totes les pàgines
    final = Pdf.new()
    for pb in result_pages:
        p = Pdf.open(io.BytesIO(pb))
        final.pages.append(final.copy_foreign(p.pages[0]))

    buf = io.BytesIO(); final.save(buf); return buf.getvalue()


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

    for pi in range(min(sides, doc.page_count)):
        page = doc[pi]
        ow,oh = page.rect.width, page.rect.height
        cwpt,chpt = pt(cw), pt(ch)
        ox0,oy0 = (ow-cwpt)/2, (oh-chpt)/2
        pix = page.get_pixmap(matrix=fitz.Matrix(scale,scale), alpha=False)
        img = Image.frombytes("RGB", [pix.width,pix.height], pix.samples)
        cx0=max(0,int(ox0*scale)); cy0=max(0,int(oy0*scale))
        cx1=min(img.width,int((ox0+cwpt)*scale))
        cy1=min(img.height,int((oy0+chpt)*scale))
        cropped = img.crop((cx0,cy0,cx1,cy1))
        bpx = int(bleed*(dpi/25.4))
        wb = extend_bleed(cropped, bpx, method)

        png = io.BytesIO(); wb.save(png,'PNG'); png_b = png.getvalue()
        ip = Pdf.new()
        st = pikepdf.Stream(ip, png_b,
            Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image,
            Width=wb.width, Height=wb.height,
            ColorSpace=pikepdf.Name.DeviceRGB,
            BitsPerComponent=8, Filter=pikepdf.Name.FlateDecode)
        imd = pikepdf.Dictionary(); imd["/Im"] = st
        ip.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name.Page, MediaBox=[0,0,TW,TH],
            Resources=pikepdf.Dictionary(XObject=imd),
            Contents=pikepdf.Stream(ip,
                f"q {TW:.3f} 0 0 {TH:.3f} 0 0 cm /Im Do Q".encode())
        )))
        ib = io.BytesIO(); ip.save(ib); ib.seek(0)
        xobj = out.copy_foreign(Pdf.open(ib).pages[0].as_form_xobject())
        xd2 = pikepdf.Dictionary(); xd2["/C"] = xobj
        res2 = pikepdf.Dictionary(XObject=xd2)
        mirror = (pi == 1); lines = []
        for r in range(rows):
            for c in range(cols):
                ci = (cols-1-c) if mirror else c
                x = sx + ci*(TW+GP)
                y = sy + (rows-1-r)*(TH+GP)
                lines.append(f"q 1 0 0 1 {x:.3f} {y:.3f} cm /C Do Q")
                if border and bw > 0:
                    bwpt = pt(bw); bc = border
                    lines.append(f"{bc[0]} {bc[1]} {bc[2]} RG {bwpt:.3f} w "
                                 f"{x:.3f} {y:.3f} {TW:.3f} {TH:.3f} re S")
        out.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name.Page, MediaBox=[0,0,SW,SH],
            Resources=res2,
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
        ow_mm = (float(mb[2])-float(mb[0]))/MM
        oh_mm = (float(mb[3])-float(mb[1]))/MM
        cw = cw or ow_mm
        ch = ch or oh_mm

        if bleed > 0:
            result = impose_raster(pdf_bytes, sw, sh, cw, ch, bleed,
                                   cols, rows, gap, sides, bcol, bw, dpi, bmet)
        else:
            result = impose_vector(pdf_bytes, sw, sh, cw, ch,
                                   cols, rows, gap, sides, bcol, bw)

        return send_file(
            io.BytesIO(result),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'impose_{cols}x{rows}.pdf'
        )
    except Exception as e:
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
