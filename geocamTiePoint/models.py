# __BEGIN_LICENSE__
# Copyright (C) 2008-2010 United States Government as represented by
# the Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# __END_LICENSE__

# avoid warnings due to pylint not understanding DotDict objects
# pylint: disable=E1101

import os
import datetime
import re
import logging
import threading
try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

import PIL.Image
import pyproj
import numpy as np
from osgeo import gdal

from django.db import models
from django.core.urlresolvers import reverse
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.conf import settings

from geocamUtil import anyjson as json
from geocamUtil import gdal2tiles, imageInfo
from geocamUtil.models.ExtrasDotField import ExtrasDotField
from geocamTiePoint import quadTree, transform, rpcModel, gdalUtil
from geocamUtil.ErrorJSONResponse import ErrorJSONResponse, checkIfErrorJSONResponse


# poor man's local memory cache for one quadtree tile generator. a
# common access pattern is that the same instance of the app gets
# multiple tile requests on the same quadtree. optimize for that case by
# keeping the generator in memory. note: an alternative approach would
# use the memcached cache, but that would get rid of much of the benefit
# in terms of serialization/deserialization.
cachedGeneratorG = threading.local()


def getNewImageFileName(instance, filename):
    return 'geocamTiePoint/overlay_images/' + filename


def getNewExportFileName(instance, filename):
    return 'geocamTiePoint/export/' + filename


def dumps(obj):
    return json.dumps(obj, sort_keys=True, indent=4)


class MissingData(object):
    pass
MISSING = MissingData()


def dosys(cmd):
    logging.info('running: %s', cmd)
    ret = os.system(cmd)
    if ret != 0:
        logging.warn('command exited with non-zero return value %s', ret)
    return ret
        

class ISSimage:
    def __init__(self, mission, roll, frame, sizeType):
        self.mission = mission
        self.roll = roll
        self.frame = frame
        self.sizeType = sizeType
        self.infoUrl = "http://eol.jsc.nasa.gov/GeoCam/PhotoInfo.pl?photo=%s-%s-%s" % (self.mission, self.roll, self.frame)
        self.imageUrl = self.__getImageUrl()
        self.width = None
        self.height = None
        # check for must have info.
        assert self.mission != ""
        assert self.roll != ""
        assert self.frame != ""
        assert self.sizeType != ""
        # set image file
        self.imageFile = imageInfo.getImageFile(self.imageUrl)
        # set extras
        self.extras = imageInfo.constructExtrasDict(self.infoUrl) 
        try:  # open it as a PIL image
            bits = self.imageFile.file.read()
            image = PIL.Image.open(StringIO(bits))
            if image.size: 
                self.width, self.height = image.size
            # set focal length
            sensorSize = (.036,.0239) #TODO: calculate this
            focalLength = imageInfo.getAccurateFocalLengths(image.size, self.extras.focalLength_unitless, sensorSize)
            self.extras['focalLength'] = [round(focalLength[0],2), round(focalLength[1],2)]        
        except Exception as e:  # pylint: disable=W0703
            logging.error("PIL failed to open image: " + str(e))
        
    def __getImageUrl(self):
        if self.sizeType == 'small':
            if (self.roll == "E") or (self.roll == "ESC"):
                rootUrl = "http://eol.jsc.nasa.gov/DatabaseImages/ESC/small" 
            else: 
                rootUrl = "http://eol.jsc.nasa.gov/DatabaseImages/ISD/lowres"
        else: 
            if (self.roll == "E") or (self.roll == "ESC"):
                rootUrl = "http://eol.jsc.nasa.gov/DatabaseImages/ESC/large" 
            else: 
                rootUrl = "http://eol.jsc.nasa.gov/DatabaseImages/ISD/highres"
        return  rootUrl + "/" + self.mission + "/" + self.mission + "-" + self.roll + "-" + self.frame + ".jpg"


class ImageData(models.Model):
    lastModifiedTime = models.DateTimeField()
    # image.max_length needs to be long enough to hold a blobstore key
    image = models.ImageField(upload_to=getNewImageFileName,
                              max_length=255)
    contentType = models.CharField(max_length=50)
    overlay = models.ForeignKey('Overlay', null=True, blank=True)
    checksum = models.CharField(max_length=128, blank=True)
    # we set unusedTime when a QuadTree is no longer referenced by an Overlay.
    # it will eventually be deleted.
    unusedTime = models.DateTimeField(null=True, blank=True)
    # If certain angle is requested and image data is available in db, 
    # we can just pull up that image.
    rotationAngle = models.IntegerField(null=True, blank=True, default=0)
    contrast = models.FloatField(null=True, blank=True, default=0)
    brightness = models.FloatField(null=True, blank=True, default=0)
    raw = models.BooleanField(default=False)

    def __unicode__(self):
        if self.overlay:
            overlay_id = self.overlay.key
        else:
            overlay_id = None
        return ('ImageData overlay_id=%s checksum=%s %s'
                % (overlay_id, self.checksum, self.lastModifiedTime))

    def save(self, *args, **kwargs):
        self.lastModifiedTime = datetime.datetime.utcnow()
        super(ImageData, self).save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.image.delete()
        super(ImageData, self).delete(*args, **kwargs)


class QuadTree(models.Model):
    lastModifiedTime = models.DateTimeField()
    imageData = models.ForeignKey('ImageData', null=True, blank=True)
    # transform is either an empty string (simple quadTree) or a JSON-formatted
    # definition of the warping transform (warped quadTree)
    transform = models.TextField(blank=True)

    # note: 'exportZip' is a bit of a misnomer since the archive may not
    # be a zipfile (tarball by default).  but no real need to change the
    # field name and force a db migration.
    htmlExportName = models.CharField(max_length=255,
                                     null=True, blank=True)
    htmlExport = models.FileField(upload_to=getNewExportFileName,
                                 max_length=255,
                                 null=True, blank=True)
    kmlExportName = models.CharField(max_length=255,
                                     null=True, blank=True)
    kmlExport = models.FileField(upload_to=getNewExportFileName,
                                 max_length=255,
                                 null=True, blank=True)
    geotiffExportName = models.CharField(max_length=255,
                                     null=True, blank=True)
    geotiffExport = models.FileField(upload_to=getNewExportFileName,
                                     max_length=255,
                                     null=True, blank=True)

    # we set unusedTime when a QuadTree is no longer referenced by an Overlay.
    # it will eventually be deleted.
    unusedTime = models.DateTimeField(null=True, blank=True)

    def __unicode__(self):
        return ('QuadTree id=%s imageData_id=%s transform=%s %s'
                % (self.id, self.imageData.id, self.transform,
                   self.lastModifiedTime))

    def save(self, *args, **kwargs):
        self.lastModifiedTime = datetime.datetime.utcnow()
        super(QuadTree, self).save(*args, **kwargs)

    def getBasePath(self):
        return settings.DATA_ROOT + 'geocamTiePoint/tiles/%d' % self.id

    def convertImageToRgbaIfNeeded(self, image):
        """
        With the latest code we convert to RGBA on image import. This
        special case helps migrate any remaining images that didn't get
        that conversion.
        """
        if image.mode != 'RGBA':
            image = image.convert('RGBA')
            out = StringIO()
            image.save(out, format='png')
            self.imageData.image.save('dummy.png', ContentFile(out.getvalue()), save=False)
            self.imageData.contentType = 'image/png'
            self.imageData.save()
    
    def getImage(self):
        # apparently image.file is not a very good file work-alike,
        # so let's delegate to StringIO(), which PIL is tested against
        bits = self.imageData.image.file.read()
        logging.info('getImage len=%s header=%s',
                     len(bits), repr(bits[:10]))
        fakeFile = StringIO(bits)

        im = PIL.Image.open(fakeFile)
        self.convertImageToRgbaIfNeeded(im)
        return im

    @classmethod
    def getGeneratorCacheKey(cls, quadTreeId):
        return 'geocamTiePoint.QuadTreeGenerator.%s' % quadTreeId

    @classmethod
    def getGeneratorWithCache(cls, quadTreeId):
        cachedGeneratorCopy = getattr(cachedGeneratorG, 'gen',
                                      {'key': None, 'value': None})
        key = cls.getGeneratorCacheKey(quadTreeId)
        if cachedGeneratorCopy['key'] == key:
            logging.debug('getGeneratorWithCache hit %s', key)
            result = cachedGeneratorCopy['value']
        else:
            logging.debug('getGeneratorWithCache miss %s', key)
            q = get_object_or_404(QuadTree, id=quadTreeId)
            result = q.getGenerator()
            cachedGeneratorG.gen = dict(key=key, value=result)
        return result

    def getGenerator(self):
        image = self.getImage()
        if self.transform:
            return quadTree.WarpedQuadTreeGenerator(self.id,
                                                   image,
                                                   json.loads(self.transform))
        else:
            return quadTree.SimpleQuadTreeGenerator(self.id,
                                                image)

    @staticmethod
    def getSimpleViewHtml(tileRootUrl, metaJson, slug):
        return render_to_string('geocamTiePoint/simple-view.html',
                                {'name': metaJson['name'],
                                 'slug': slug,
                                 'tileRootUrl': tileRootUrl,
                                 'bounds': dumps(metaJson['bounds']),
                                 'tileUrlTemplate': '%s/[ZOOM]/[X]/[Y].png' % slug,
                                 'tileSize': 256})

    def reversePts(self, toPts):
        """
        Helper needed for fitRpcToModel. 
        Does v = T(u).
            @v is a 3 x n matrix of n 3D points in WGS84 (lon, lat, alt)
            @u is a 2 x n matrix of n 2D points in image (px, py)
        """
        transformDict  = json.loads(self.transform)
        tform =  transform.makeTransform(transformDict)
        pixels = None
        for column in toPts.T:
            # convert column (3D pts in WGS84) to gmap meters
            lonlat = column[:2]
            gmap_meters = transform.lonLatToMeters(lonlat)
            px, py = tform.reverse(gmap_meters)
            newCol = np.array([[px],[py]])
            if pixels is None:
                pixels = newCol
            else:
                pixels = np.column_stack((pixels, newCol))
        return pixels        
    
    def getImageSizeType(self):
        imgWidth = self.imageData.overlay.extras.imageSize[0]
        imgSize = "large"
        if imgWidth < 1200:
            imgSize = "small"   
        return imgSize

    def generateHtmlExport(self, exportName, metaJson, slug):
        imgSize = self.getImageSizeType()
        gen = self.getGeneratorWithCache(self.id)
        now = datetime.datetime.utcnow()
        timestamp = now.strftime('%Y-%m-%d-%H%M%S-UTC')
        # generate html export
        htmlExportName = exportName + ('-%s-html_%s' % (imgSize, timestamp))
        viewHtmlPath = 'view.html'
        tileRootUrl = './%s' % slug
        html = self.getSimpleViewHtml(tileRootUrl, metaJson, slug)
        logging.debug('html: len=%s head=%s', len(html), repr(html[:10]))
        # tar the html export
        writer = quadTree.TarWriter(htmlExportName)
        gen.writeQuadTree(writer, slug)
        writer.writeData(viewHtmlPath, html)
        writer.writeData('meta.json', dumps(metaJson))
        self.htmlExportName = '%s.tar.gz' % htmlExportName
        self.htmlExport.save(self.htmlExportName,
                            ContentFile(writer.getData()))

        
    def generateGeotiffExport(self, exportName, metaJson, slug):
        """
        This generates a geotiff from RPC.
        """
        imgSize = self.getImageSizeType()
        now = datetime.datetime.utcnow()
        timestamp = now.strftime('%Y-%m-%d-%H%M%S-UTC')
        
        # get image width and height
        overlay = Overlay.objects.get(alignedQuadTree = self) 
        imageWidth, imageHeight = overlay.extras.imageSize
        
        # update the center point with current transform and use those values
        transformDict  = overlay.extras.transform
        tform =  transform.makeTransform(transformDict)
        center_meters = tform.forward([imageWidth / 2, imageHeight / 2])
        clon, clat  = transform.metersToLatLon(center_meters)
        # get the RPC values 
        T_rpc = rpcModel.fitRpcToModel(self.reversePts, 
                                     imageWidth, imageHeight,
                                     clon, clat)
        srs = gdalUtil.EPSG_4326
        # get original image
        imgPath = overlay.getRawImageData().image.url.replace('/data/', settings.DATA_ROOT)
        # reproject and tar the output tiff
        geotiffExportName = exportName + ('-%s-geotiff_%s' % (imgSize, timestamp))
        geotiffFolderPath = settings.DATA_ROOT + 'geocamTiePoint/export/' + geotiffExportName
        dosys('mkdir %s' % geotiffFolderPath)

        fullFilePath = geotiffFolderPath + '/' + geotiffExportName +'.tif'
        gdalUtil.reprojectWithRpcMetadata(imgPath, T_rpc.getVrtMetadata(), srs, fullFilePath)

        geotiff_writer = quadTree.TarWriter(geotiffExportName)
        arcName = geotiffExportName + '.tif'
        geotiff_writer.addFile(fullFilePath, geotiffExportName + '/' + arcName)  # double check this line (second arg may not be necessary)
        self.geotiffExportName = '%s.tar.gz' % geotiffExportName
        self.geotiffExport.save(self.geotiffExportName,
                                ContentFile(geotiff_writer.getData()))

    
    def generateKmlExport(self, exportName, metaJson, slug):
        """
        this generates the kml and the tiles.
        """
        imgSize = self.getImageSizeType()
        now = datetime.datetime.utcnow()
        timestamp = now.strftime('%Y-%m-%d-%H%M%S-UTC')
        
        kmlExportName = exportName + ('-%s-kml_%s' % (imgSize, timestamp))
        kmlFolderPath = settings.DATA_ROOT + 'geocamTiePoint/export/' + kmlExportName
        
        # get the path to latest geotiff file
        inputFile = self.geotiffExportName.replace('.tar.gz', '')
        inputFile = settings.DATA_ROOT + 'geocamTiePoint/export/' + inputFile + '/' + inputFile + ".tif"
        #TODO: make this call the gdal2tiles
        
        g2t = gdal2tiles.GDAL2Tiles(["--force-kml", str(inputFile), str(kmlFolderPath)])
        g2t.process()
        
        # tar the kml
        kml_writer = quadTree.TarWriter(kmlExportName)
        kml_writer.addFile(kmlFolderPath, kmlExportName)  # double check. second arg may not be necessary
        self.kmlExportName = '%s.tar.gz' % kmlExportName
        self.kmlExport.save(self.kmlExportName, 
                            ContentFile(kml_writer.getData()))      
    
        
class Overlay(models.Model):
    # required fields 
    key = models.AutoField(primary_key=True, unique=True)
    lastModifiedTime = models.DateTimeField()
    name = models.CharField(max_length=50)
    
    # optional fields
    # author: user who owns this overlay in the system
    author = models.ForeignKey(User, null=True, blank=True)
    description = models.TextField(blank=True)
    imageSourceUrl = models.URLField(blank=True) #, verify_exists=False)
    imageData = models.ForeignKey(ImageData, null=True, blank=True,
                                  related_name='currentOverlays',
                                  on_delete=models.SET_NULL)
    unalignedQuadTree = models.ForeignKey(QuadTree, null=True, blank=True,
                                          related_name='unalignedOverlays',
                                          on_delete=models.SET_NULL)
    alignedQuadTree = models.ForeignKey(QuadTree, null=True, blank=True,
                                        related_name='alignedOverlays',
                                        on_delete=models.SET_NULL)
    isPublic = models.BooleanField(default=settings.GEOCAM_TIE_POINT_PUBLIC_BY_DEFAULT)
    coverage = models.CharField(max_length=255, blank=True,
                                verbose_name='Name of region covered by the overlay')
    # creator: name of person or organization who should get the credit
    # for producing the overlay
    creator = models.CharField(max_length=255, blank=True)
    sourceDate = models.CharField(max_length=255, blank=True,
                                  verbose_name='Source image creation date')
    rights = models.CharField(max_length=255, blank=True,
                              verbose_name='Copyright information')
    license = models.URLField(blank=True,
                              verbose_name='License permitting reuse (optional)',
                              choices=settings.GEOCAM_TIE_POINT_LICENSE_CHOICES)
    # stores mission roll frame of the image. i.e. "ISS039-E-12345"
    issMRF = models.CharField(max_length=255, null=True, blank=True,
                              help_text="Please use the following format: <em>[Mission ID]-[Roll]-[Frame number]</em>") # ISS mission roll frame id of image.
    # extras: a special JSON-format field that holds additional
    # schema-free fields in the overlay model. Members of the field can
    # be accessed using dot notation. currently used extras subfields
    # include: imageSize, points, transform, bounds, centerLat, centerLon, rotatedImageSize
    extras = ExtrasDotField()
    # import/export configuration
    exportFields = ('key', 'lastModifiedTime', 'name', 'description', 'imageSourceUrl', 
                    'issMRF', 'centerLat', 'centerLon', 'creator')
    importFields = ('name', 'description', 'imageSourceUrl')
    importExtrasFields = ('points', 'transform', 'centerLat', 'centerLon')
    

    def getRawImageData(self):
        """
        Returns the original image data created upon image upload (not rotated, not enhanced)
        """
        imageData = ImageData.objects.filter(overlay__key = self.key).filter(raw = True)
        return imageData[0]

    def getAlignedTilesUrl(self):
        if self.isPublic:
            urlName = 'geocamTiePoint_publicTile'
        else:
            urlName = 'geocamTiePoint_tile'
        return reverse(urlName,
                       args=[str(self.alignedQuadTree.id)])

    def getJsonDict(self):
        # export all schema-free subfields of extras
        result = self.extras.copy()
        # export other schema-controlled fields of self (listed in exportFields)
        for key in self.exportFields:
            val = getattr(self, key, None)
            if val not in ('', None):
                result[key] = val
        # conversions
        result['lmt_datetime'] = result['lastModifiedTime'].strftime('%F %k:%M')
        result['lastModifiedTime'] = (result['lastModifiedTime']
                                      .replace(microsecond=0)
                                      .isoformat()
                                      + 'Z')
        # calculate and export urls for client convenience
        result['url'] = reverse('geocamTiePoint_overlayIdJson', args=[self.key])
        if self.unalignedQuadTree is not None:
            result['unalignedTilesUrl'] = reverse('geocamTiePoint_tile',
                                                  args=[str(self.unalignedQuadTree.id)])
            result['unalignedTilesZoomOffset'] = quadTree.ZOOM_OFFSET
        if self.alignedQuadTree is not None:
            result['alignedTilesUrl'] = self.getAlignedTilesUrl()

            # note: when exportZip has not been set, its value is not
            # None but <FieldFile: None>, which is False in bool() context
            if self.alignedQuadTree.htmlExport: 
                result['htmlExportUrl'] = reverse('geocamTiePoint_overlayExport',
                                                  args=[self.key, 
                                                        'html',
                                                        str(self.alignedQuadTree.htmlExportName)])
            if self.alignedQuadTree.kmlExport: 
                result['kmlExportUrl'] = reverse('geocamTiePoint_overlayExport',
                                              args=[self.key,
                                                    'kml',
                                                    str(self.alignedQuadTree.kmlExportName)])
            if self.alignedQuadTree.geotiffExport: 
                result['geotiffExportUrl'] = reverse('geocamTiePoint_overlayExport',
                                              args=[self.key,
                                                    'geotiff',
                                                    str(self.alignedQuadTree.geotiffExportName)])
        return result

    def setJsonDict(self, jsonDict):
        # set schema-controlled fields of self (listed in
        # self.importFields)
        for key in self.importFields:
            val = jsonDict.get(key, MISSING)
            if val is not MISSING:
                setattr(self, key, val)

        # set schema-free subfields of self.extras (listed in
        # self.importExtrasFields)
        for key in self.importExtrasFields:
            val = jsonDict.get(key, MISSING)
            if val is not MISSING:
                self.extras[key] = val

    jsonDict = property(getJsonDict, setJsonDict)

    class Meta:
        ordering = ['-key']

    def __unicode__(self):
        return ('Overlay key=%s name=%s author=%s %s'
                % (self.key, self.name, self.author.username,
                   self.lastModifiedTime))

    def save(self, *args, **kwargs):
        self.lastModifiedTime = datetime.datetime.utcnow()
        super(Overlay, self).save(*args, **kwargs)

    def getSlug(self):
        return re.sub('[^\w]', '_', os.path.splitext(self.name)[0])

    def getExportName(self):
        now = datetime.datetime.utcnow()
        return 'mapfasten-%s' % self.getSlug()

    def generateUnalignedQuadTree(self):
        qt = QuadTree(imageData=self.imageData)
        qt.save()

        self.unalignedQuadTree = qt
        self.save()

        return qt

    def generateAlignedQuadTree(self):
        if self.extras.get('transform') is None:
            return None
        # grab the original image's imageData
        originalImageData = self.getRawImageData()
        qt = QuadTree(imageData=originalImageData,
                    transform=dumps(self.extras.transform))
        qt.save()
        self.alignedQuadTree = qt
        return qt

    def generateHtmlExport(self):
        (self.alignedQuadTree.generateHtmlExport
         (self.getExportName(),
          self.getJsonDict(),
          self.getSlug()))
        return self.alignedQuadTree.htmlExport 

    def generateKmlExport(self):
        (self.alignedQuadTree.generateKmlExport
         (self.getExportName(),
          self.getJsonDict(),
          self.getSlug()))
        return self.alignedQuadTree.kmlExport 

    def generateGeotiffExport(self):
        (self.alignedQuadTree.generateGeotiffExport
         (self.getExportName(),
          self.getJsonDict(),
          self.getSlug()))
        return self.alignedQuadTree.geotiffExport 

    def updateAlignment(self):
        toPts, fromPts = transform.splitPoints(self.extras.points)
        tform = transform.getTransform(toPts, fromPts)
        self.extras.transform = tform.getJsonDict()

    def getSimpleAlignedOverlayViewer(self, request):
        alignedTilesPath = re.sub(r'/\[ZOOM\].*$', '', self.getAlignedTilesUrl())
        alignedTilesRootUrl = request.build_absolute_uri(alignedTilesPath)
        return (self.alignedQuadTree
                .getSimpleViewHtml(alignedTilesRootUrl,
                                   self.getJsonDict(),
                                   self.getSlug()))
        
#########################################
# models for autoregistration pipeline  #
#########################################
class IssTelemetry(models.Model):
    issMRF = models.CharField(max_length=255, null=True, blank=True, help_text="Please use the following format: <em>[Mission ID]-[Roll]-[Frame number]</em>") 
    x = models.FloatField(null=True, blank=True, default=0)
    y = models.FloatField(null=True, blank=True, default=0)
    z = models.FloatField(null=True, blank=True, default=0)
    r = models.FloatField(null=True, blank=True, default=0)
    p = models.FloatField(null=True, blank=True, default=0)
    y = models.FloatField(null=True, blank=True, default=0)


class AutomatchResults(models.Model):
    issMRF = models.CharField(max_length=255, help_text="Please use the following format: <em>[Mission ID]-[Roll]-[Frame number]</em>") 
    lon = models.FloatField(null=True, blank=True, default=0, help_text="longitude (world coordinates)")
    lat = models.FloatField(null=True, blank=True, default=0, help_text="latitude (world coordinates)")
    px = models.IntegerField(null=True, blank=True, default=0, help_text="pixel coordinates")
    py = models.IntegerField(null=True, blank=True, default=0, help_text="pixel coordinates")
    matchedImageId = models.CharField(max_length=255, blank=True)
    matchConfidence = models.CharField(max_length=255, blank=True)
    matchDate = models.DateTimeField(null=True, blank=True)
    centerPointSource = models.CharField(max_length=255, blank=True, help_text="source of center point. Either curated, CEO, GeoSens, or Nadir")
    

class GeoSens(models.Model):
    issMRF = models.CharField(max_length=255, help_text="Please use the following format: <em>[Mission ID]-[Roll]-[Frame number]</em>") 
    r = models.FloatField(null=True, blank=True, default=0)
    p = models.FloatField(null=True, blank=True, default=0)
    y = models.FloatField(null=True, blank=True, default=0)