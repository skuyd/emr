import apps.processing.models
import django.core.validators
import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('documents', '0002_processingrun_lease_token'),
    ]

    operations = [
        migrations.CreateModel(
            name='ParsingVersion',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('parser_version', models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(message='Version identifiers must be stable, printable identifiers.', regex='^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')])),
                ('ocr_provider', models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(message='Version identifiers must be stable, printable identifiers.', regex='^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')])),
                ('ocr_provider_version', models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(message='Version identifiers must be stable, printable identifiers.', regex='^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')])),
                ('dictionary_version', models.CharField(blank=True, max_length=64, validators=[django.core.validators.RegexValidator(message='Version identifiers must be stable, printable identifiers.', regex='^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')])),
                ('dictionary_hash', models.CharField(blank=True, max_length=64, validators=[django.core.validators.RegexValidator(message='A lowercase SHA-256 digest is required.', regex='^[0-9a-f]{64}$')])),
                ('status', models.CharField(choices=[('BUILDING', 'Building'), ('READY', 'Ready'), ('PUBLISHED', 'Published'), ('FAILED', 'Failed')], default='BUILDING', max_length=12)),
                ('active', models.BooleanField(default=False)),
                ('diagnostics', models.JSONField(blank=True, default=dict)),
                ('published_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('document', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='parsing_versions', to='documents.document')),
                ('processing_run', models.OneToOneField(on_delete=django.db.models.deletion.RESTRICT, related_name='parsing_version', to='documents.processingrun')),
            ],
        ),
        migrations.CreateModel(
            name='OcrBlock',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('reading_order', models.PositiveIntegerField()),
                ('text', models.TextField()),
                ('polygon', models.JSONField(validators=[apps.processing.models.validate_normalized_polygon])),
                ('confidence', models.DecimalField(decimal_places=4, max_digits=5, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(1)])),
                ('provider_metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('document_page', models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='ocr_blocks', to='documents.documentpage')),
                ('parsing_version', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ocr_blocks', to='processing.parsingversion')),
            ],
        ),
        migrations.CreateModel(
            name='SourceEvidence',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('polygon', models.JSONField(blank=True, null=True, validators=[apps.processing.models.validate_normalized_polygon])),
                ('source_text', models.TextField(blank=True)),
                ('confidence', models.DecimalField(blank=True, decimal_places=4, max_digits=5, null=True, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(1)])),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('document_page', models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='source_evidence', to='documents.documentpage')),
                ('ocr_block', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='evidence', to='processing.ocrblock')),
                ('parsing_version', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='source_evidence', to='processing.parsingversion')),
            ],
        ),
        migrations.CreateModel(
            name='DocumentMetadataCandidate',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('DOCUMENT_TYPE', 'Document type'), ('DOCUMENT_DATE', 'Document date'), ('INSTITUTION', 'Institution')], max_length=24)),
                ('raw_text', models.TextField()),
                ('normalized_value', models.CharField(max_length=512)),
                ('precision', models.CharField(choices=[('UNKNOWN', 'Unknown'), ('YEAR', 'Year'), ('MONTH', 'Month'), ('DAY', 'Day')], default='UNKNOWN', max_length=12)),
                ('confidence', models.DecimalField(decimal_places=4, max_digits=5, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(1)])),
                ('selected', models.BooleanField(default=False)),
                ('rationale', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('parsing_version', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='metadata_candidates', to='processing.parsingversion')),
                ('evidence', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='metadata_candidates', to='processing.sourceevidence')),
            ],
        ),
        migrations.AddIndex(
            model_name='parsingversion',
            index=models.Index(fields=['document', 'active'], name='processing_doc_active'),
        ),
        migrations.AddIndex(
            model_name='parsingversion',
            index=models.Index(fields=['status', 'created_at'], name='processing_version_status'),
        ),
        migrations.AddConstraint(
            model_name='parsingversion',
            constraint=models.UniqueConstraint(condition=models.Q(('active', True)), fields=('document',), name='processing_one_active_version'),
        ),
        migrations.AddConstraint(
            model_name='parsingversion',
            constraint=models.CheckConstraint(condition=models.Q(('active', False), models.Q(('active', True), ('status', 'PUBLISHED'), ('published_at__isnull', False)), _connector='OR'), name='processing_active_is_published'),
        ),
        migrations.AddIndex(
            model_name='ocrblock',
            index=models.Index(fields=['parsing_version', 'document_page'], name='processing_ocr_page'),
        ),
        migrations.AddConstraint(
            model_name='ocrblock',
            constraint=models.UniqueConstraint(fields=('parsing_version', 'document_page', 'reading_order'), name='processing_ocr_page_order'),
        ),
        migrations.AddIndex(
            model_name='sourceevidence',
            index=models.Index(fields=['parsing_version', 'document_page'], name='processing_evidence_page'),
        ),
        migrations.AddIndex(
            model_name='documentmetadatacandidate',
            index=models.Index(fields=['parsing_version', 'kind', '-confidence'], name='processing_metadata_rank'),
        ),
    ]
