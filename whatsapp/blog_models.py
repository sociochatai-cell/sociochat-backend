"""
Blog CMS Models
================

Database models for the Visual Blog CMS.
Stores blog posts with block-based content as JSONB.
"""

from datetime import datetime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON as SAJSON
from models import db, User, Workspace


class BlogPostStatus:
    """Blog post status states."""
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


# Editor capability limits (for documentation and validation)
EDITOR_LIMITS = {
    "max_blocks_per_post": 100,
    "max_columns": 4,
    "max_image_size_mb": 5,
    "max_video_size_mb": 50,
    "allowed_fonts": [
        "Inter", "Roboto", "Open Sans", "Lato", "Montserrat",
        "Poppins", "Raleway", "Oswald", "Source Sans Pro", "Nunito",
        "Playfair Display", "Merriweather", "Georgia", "Times New Roman"
    ],
    # NO custom CSS allowed
    # NO JavaScript injection allowed
    # NO arbitrary HTML allowed
}


# Allowed block types
ALLOWED_BLOCK_TYPES = [
    "heading",
    "paragraph", 
    "image",
    "section",
    "columns",
    "button",
    "embed",
    "divider",
    "quote",
    "list",
]


class BlogPost(db.Model):
    """
    Blog post model with block-based content.
    
    Content is stored as JSONB array of blocks, each with:
    - id: unique block identifier
    - type: block type (heading, paragraph, image, etc.)
    - style: styling properties (font, color, padding, etc.)
    - data: block-specific data
    - order: display order
    """
    __tablename__ = "blog_posts"
    __table_args__ = (
        db.UniqueConstraint("workspace_id", "slug", name="uq_blog_workspace_slug"),
        {"extend_existing": True}
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces2.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    
    # Post metadata
    title = db.Column(db.String(500), nullable=False)
    slug = db.Column(db.String(500), nullable=False, index=True)
    excerpt = db.Column(db.Text, nullable=True)
    featured_image = db.Column(db.String(1000), nullable=True)
    
    # Status and versioning
    status = db.Column(db.String(20), nullable=False, default=BlogPostStatus.DRAFT, index=True)
    render_version = db.Column(db.Integer, nullable=False, default=1)  # For future-proofing rendering
    
    # Content storage
    content_json = db.Column(SAJSON().with_variant(JSONB, "postgresql"), nullable=False, default=list)  # Draft content (current edits)
    published_content_json = db.Column(SAJSON().with_variant(JSONB, "postgresql"), nullable=True)  # Frozen snapshot for live site
    
    # SEO metadata
    seo_meta = db.Column(SAJSON().with_variant(JSONB, "postgresql"), nullable=True, default=dict)
    
    # Timestamps
    published_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    workspace = db.relationship(Workspace, backref=db.backref("blog_posts", lazy="dynamic"))
    author = db.relationship(User, backref=db.backref("blog_posts", lazy="dynamic"))

    def to_dict(self, include_content=True):
        """Convert to dictionary for API response."""
        # Query author if needed
        author_name = None
        author_email = None
        if self.author_id:
            from models import User
            author = User.query.get(self.author_id)
            if author:
                author_name = author.name
                author_email = author.email
        
        data = {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "author_id": self.author_id,
            "author_name": author_name,
            "author_email": author_email,
            "title": self.title,
            "slug": self.slug,
            "excerpt": self.excerpt,
            "featured_image": self.featured_image,
            "status": self.status,
            "render_version": self.render_version,
            "seo_meta": self.seo_meta or {},
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        
        if include_content:
            # For published posts, serve published_content_json if available
            if self.status == BlogPostStatus.PUBLISHED and self.published_content_json:
                data["content"] = self.published_content_json
            else:
                data["content"] = self.content_json or []
        
        return data

    def publish(self):
        """Publish the post, freezing current content."""
        self.published_content_json = self.content_json
        self.status = BlogPostStatus.PUBLISHED
        self.published_at = datetime.utcnow()

    def unpublish(self):
        """Unpublish the post back to draft."""
        self.status = BlogPostStatus.DRAFT

    @staticmethod
    def generate_unique_slug(workspace_id: int, base_slug: str, exclude_id: int = None) -> str:
        """
        Generate a unique slug for a workspace.
        
        If the base slug exists, appends -1, -2, etc.
        """
        import re
        
        # Sanitize slug
        slug = re.sub(r'[^a-z0-9-]', '', base_slug.lower().replace(' ', '-'))
        slug = re.sub(r'-+', '-', slug).strip('-')
        
        if not slug:
            slug = "untitled"
        
        # Check for existing
        query = BlogPost.query.filter_by(workspace_id=workspace_id, slug=slug)
        if exclude_id:
            query = query.filter(BlogPost.id != exclude_id)
        
        if not query.first():
            return slug
        
        # Find unique suffix
        counter = 1
        while True:
            new_slug = f"{slug}-{counter}"
            query = BlogPost.query.filter_by(workspace_id=workspace_id, slug=new_slug)
            if exclude_id:
                query = query.filter(BlogPost.id != exclude_id)
            if not query.first():
                return new_slug
            counter += 1


def validate_block(block: dict) -> tuple[bool, str]:
    """
    Validate a single content block.
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(block, dict):
        return False, "Block must be a dictionary"
    
    required_keys = ["id", "type", "style", "data"]
    for key in required_keys:
        if key not in block:
            return False, f"Block missing required key: {key}"
    
    if block["type"] not in ALLOWED_BLOCK_TYPES:
        return False, f"Invalid block type: {block['type']}"
    
    if not isinstance(block["style"], dict):
        return False, "Block style must be a dictionary"
    
    if not isinstance(block["data"], dict):
        return False, "Block data must be a dictionary"
    
    return True, ""


def validate_content_json(content: list) -> tuple[bool, list[str]]:
    """
    Validate entire content_json array.
    
    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    if not isinstance(content, list):
        return False, ["content_json must be an array"]
    
    if len(content) > EDITOR_LIMITS["max_blocks_per_post"]:
        return False, [f"Too many blocks. Maximum: {EDITOR_LIMITS['max_blocks_per_post']}"]
    
    errors = []
    for i, block in enumerate(content):
        is_valid, error = validate_block(block)
        if not is_valid:
            errors.append(f"Block {i}: {error}")
    
    return len(errors) == 0, errors
