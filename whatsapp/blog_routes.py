"""
Blog CMS Routes
================

API endpoints for the Visual Blog CMS.
Includes RBAC middleware, CRUD operations, publish workflow, and public access.
"""

import os
import logging
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, g
from sqlalchemy import or_

from models import db, User, Workspace
from .blog_models import (
    BlogPost, 
    BlogPostStatus, 
    EDITOR_LIMITS, 
    ALLOWED_BLOCK_TYPES,
    validate_content_json
)

logger = logging.getLogger(__name__)

blog_bp = Blueprint("blog", __name__, url_prefix="/api/blog")

# Email whitelist for marketing admins (fallback if role not set)
MARKETING_ADMIN_EMAILS = set(
    email.strip().lower() 
    for email in os.environ.get("MARKETING_ADMIN_EMAIL", "").split(",") 
    if email.strip()
)


# ============================================================
# Authentication Helpers
# ============================================================

def get_current_user():
    """Current user from the server session or a SIGNED Bearer JWT only.

    The forgeable X-User-Id header fallback was removed (see auth_core)."""
    from auth_core import authenticated_user_id
    user_id = authenticated_user_id()
    if not user_id:
        return None
    try:
        return User.query.get(int(user_id))
    except (ValueError, TypeError):
        return None


def get_current_admin():
    """Current admin from the server session or a SIGNED admin Bearer JWT only.

    The forgeable X-Admin-Id header and ?admin_id= query param (either of which
    previously granted platform-admin access to anyone) were removed."""
    from auth_core import authenticated_admin_id
    from models import Admin
    admin_id = authenticated_admin_id()
    if not admin_id:
        return None
    try:
        return Admin.query.get(int(admin_id))
    except (ValueError, TypeError):
        return None


def require_blog_access(f):
    """
    Decorator to require blog access.
    
    Enforces:
    1. Admin portal access (admin=true with valid admin session), OR
    2. User is authenticated with workspace ownership
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # Check for admin portal access first (admin=true parameter)
        admin_mode = request.args.get("admin", "").lower() == "true"
        if admin_mode:
            admin = get_current_admin()
            if admin:
                # Admin portal user authenticated - allow full access
                g.admin = admin
                g.user = None  # No user context in admin mode
                logger.info(f"Admin portal access granted to admin {admin.id}")
                return f(*args, **kwargs)
            else:
                logger.warning("Admin mode requested but no valid admin session")
                return jsonify({"error": "Unauthorized", "message": "Admin authentication required"}), 401
        
        # Regular user authentication
        user = get_current_user()
        
        if not user:
            logger.warning("Blog access denied: No authenticated user")
            return jsonify({"error": "Unauthorized", "message": "Please log in"}), 401
        
        # Check if user has admin role
        user_role = getattr(user, 'role', 'user')
        is_admin = user_role == "admin"
        
        # Get workspace_id from args, view_args, or json
        workspace_id = (
            request.args.get("workspace_id") or 
            request.view_args.get("workspace_id") or
            (request.get_json(silent=True) or {}).get("workspace_id")
        )
        
        # Check workspace ownership if workspace_id is present
        is_workspace_owner = False
        if workspace_id:
            try:
                workspace_id = int(workspace_id)
                # Check if user owns this workspace
                workspace = Workspace.query.filter_by(id=workspace_id, user_id=user.id).first()
                if workspace:
                    g.workspace = workspace
                    is_workspace_owner = True
                elif not is_admin:
                    # If not owner and not admin, deny immediately if workspace specific
                    logger.warning(f"Workspace {workspace_id} access denied for user {user.id}")
                    return jsonify({
                        "error": "Forbidden",
                        "message": "Workspace access denied"
                    }), 403
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid workspace_id"}), 400
        
        # Allow if admin OR workspace owner
        if not is_admin and not is_workspace_owner:
            logger.warning(f"Blog access denied for user {user.id} (role: {user_role})")
            return jsonify({
                "error": "Forbidden", 
                "message": "Blog feature requires admin role or workspace ownership."
            }), 403
        
        g.user = user
        return f(*args, **kwargs)
    
    return decorated


# ============================================================
# Blog Post CRUD
# ============================================================

@blog_bp.route("/posts", methods=["GET"])
@require_blog_access
def list_posts():
    """List blog posts for a workspace or all workspaces (admin mode)."""
    workspace_id = request.args.get("workspace_id", type=int)
    admin_mode = request.args.get("admin", "").lower() == "true"
    
    # Admin mode allows listing all posts without workspace filter
    if not workspace_id and not admin_mode:
        return jsonify({"error": "workspace_id is required (or use admin=true for all posts)"}), 400
    
    # Filters
    status = request.args.get("status")
    search = request.args.get("search", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)
    
    query = BlogPost.query
    
    if workspace_id:
        query = query.filter_by(workspace_id=workspace_id)
    
    if status and status in [BlogPostStatus.DRAFT, BlogPostStatus.PUBLISHED, BlogPostStatus.ARCHIVED]:
        query = query.filter_by(status=status)
    
    if search:
        query = query.filter(
            or_(
                BlogPost.title.ilike(f"%{search}%"),
                BlogPost.slug.ilike(f"%{search}%")
            )
        )
    
    query = query.order_by(BlogPost.updated_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        "posts": [p.to_dict(include_content=False) for p in pagination.items],
        "pagination": {
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "pages": pagination.pages,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        }
    })


@blog_bp.route("/posts/<int:post_id>", methods=["GET"])
@require_blog_access
def get_post(post_id):
    """Get a single blog post."""
    workspace_id = request.args.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    
    post = BlogPost.query.filter_by(id=post_id, workspace_id=workspace_id).first()
    if not post:
        return jsonify({"error": "Post not found"}), 404
    
    return jsonify({"post": post.to_dict()})


@blog_bp.route("/posts", methods=["POST"])
@require_blog_access
def create_post():
    """Create a new blog post."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400
    
    workspace_id = data.get("workspace_id")
    title = data.get("title", "").strip()
    
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    if not title:
        return jsonify({"error": "title is required"}), 400
    
    # Generate unique slug
    base_slug = data.get("slug", title)
    slug = BlogPost.generate_unique_slug(workspace_id, base_slug)
    
    # Validate content if provided
    content_json = data.get("content_json", [])
    if content_json:
        is_valid, errors = validate_content_json(content_json)
        if not is_valid:
            return jsonify({"error": "Invalid content", "details": errors}), 400
    
    post = BlogPost(
        workspace_id=workspace_id,
        author_id=g.user.id,
        title=title,
        slug=slug,
        excerpt=data.get("excerpt"),
        featured_image=data.get("featured_image"),
        content_json=content_json,
        seo_meta=data.get("seo_meta", {}),
        status=BlogPostStatus.DRAFT,
    )
    
    db.session.add(post)
    db.session.commit()
    
    logger.info(f"Blog post created: {post.id} by user {g.user.id}")
    return jsonify({"post": post.to_dict()}), 201


@blog_bp.route("/posts/<int:post_id>", methods=["PUT"])
@require_blog_access
def update_post(post_id):
    """Update an existing blog post."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400
    
    workspace_id = data.get("workspace_id")
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    
    post = BlogPost.query.filter_by(id=post_id, workspace_id=workspace_id).first()
    if not post:
        return jsonify({"error": "Post not found"}), 404
    
    # Update fields
    if "title" in data:
        post.title = data["title"].strip()
    
    if "slug" in data:
        new_slug = BlogPost.generate_unique_slug(
            workspace_id, 
            data["slug"], 
            exclude_id=post_id
        )
        post.slug = new_slug
    
    if "excerpt" in data:
        post.excerpt = data["excerpt"]
    
    if "featured_image" in data:
        post.featured_image = data["featured_image"]
    
    if "content_json" in data:
        content_json = data["content_json"]
        is_valid, errors = validate_content_json(content_json)
        if not is_valid:
            return jsonify({"error": "Invalid content", "details": errors}), 400
        post.content_json = content_json
    
    if "seo_meta" in data:
        post.seo_meta = data["seo_meta"]
    
    db.session.commit()
    
    logger.info(f"Blog post updated: {post.id} by user {g.user.id}")
    return jsonify({"post": post.to_dict()})


@blog_bp.route("/posts/<int:post_id>", methods=["DELETE"])
@require_blog_access
def delete_post(post_id):
    """Delete a blog post."""
    workspace_id = request.args.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    
    post = BlogPost.query.filter_by(id=post_id, workspace_id=workspace_id).first()
    if not post:
        return jsonify({"error": "Post not found"}), 404
    
    db.session.delete(post)
    db.session.commit()
    
    logger.info(f"Blog post deleted: {post_id} by user {g.user.id}")
    return jsonify({"message": "Post deleted"})


# ============================================================
# Publish / Unpublish
# ============================================================

@blog_bp.route("/posts/<int:post_id>/publish", methods=["POST"])
@require_blog_access
def publish_post(post_id):
    """Publish a blog post."""
    data = request.get_json() or {}
    workspace_id = data.get("workspace_id") or request.args.get("workspace_id", type=int)
    
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    
    post = BlogPost.query.filter_by(id=post_id, workspace_id=workspace_id).first()
    if not post:
        return jsonify({"error": "Post not found"}), 404
    
    if not post.content_json or len(post.content_json) == 0:
        return jsonify({"error": "Cannot publish empty post"}), 400
    
    post.publish()
    db.session.commit()
    
    logger.info(f"Blog post published: {post.id} by user {g.user.id}")
    return jsonify({"post": post.to_dict()})


@blog_bp.route("/posts/<int:post_id>/unpublish", methods=["POST"])
@require_blog_access
def unpublish_post(post_id):
    """Unpublish a blog post back to draft."""
    data = request.get_json() or {}
    workspace_id = data.get("workspace_id") or request.args.get("workspace_id", type=int)
    
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    
    post = BlogPost.query.filter_by(id=post_id, workspace_id=workspace_id).first()
    if not post:
        return jsonify({"error": "Post not found"}), 404
    
    post.unpublish()
    db.session.commit()
    
    logger.info(f"Blog post unpublished: {post.id} by user {g.user.id}")
    return jsonify({"post": post.to_dict()})


@blog_bp.route("/posts/<int:post_id>/duplicate", methods=["POST"])
@require_blog_access
def duplicate_post(post_id):
    """Duplicate a blog post as draft."""
    data = request.get_json() or {}
    workspace_id = data.get("workspace_id") or request.args.get("workspace_id", type=int)
    
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    
    original = BlogPost.query.filter_by(id=post_id, workspace_id=workspace_id).first()
    if not original:
        return jsonify({"error": "Post not found"}), 404
    
    new_slug = BlogPost.generate_unique_slug(workspace_id, f"{original.slug}-copy")
    
    duplicate = BlogPost(
        workspace_id=workspace_id,
        author_id=g.user.id,
        title=f"{original.title} (Copy)",
        slug=new_slug,
        excerpt=original.excerpt,
        featured_image=original.featured_image,
        content_json=original.content_json,
        seo_meta=original.seo_meta,
        status=BlogPostStatus.DRAFT,
    )
    
    db.session.add(duplicate)
    db.session.commit()
    
    logger.info(f"Blog post duplicated: {original.id} -> {duplicate.id} by user {g.user.id}")
    return jsonify({"post": duplicate.to_dict()}), 201


# ============================================================
# Public Blog Access (no auth required)
# ============================================================

@blog_bp.route("/public/<slug>", methods=["GET"])
def get_public_post(slug):
    """Get a published blog post by slug (public access)."""
    workspace_id = request.args.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    
    post = BlogPost.query.filter_by(
        workspace_id=workspace_id,
        slug=slug,
        status=BlogPostStatus.PUBLISHED
    ).first()
    
    if not post:
        return jsonify({"error": "Post not found"}), 404
    
    return jsonify({"post": post.to_dict()})


@blog_bp.route("/public/list", methods=["GET"])
def list_public_posts():
    """List published blog posts (public access)."""
    workspace_id = request.args.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 10, type=int), 50)
    
    query = BlogPost.query.filter_by(
        workspace_id=workspace_id,
        status=BlogPostStatus.PUBLISHED
    ).order_by(BlogPost.published_at.desc())
    
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        "posts": [p.to_dict(include_content=False) for p in pagination.items],
        "pagination": {
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "pages": pagination.pages,
        }
    })


# ============================================================
# Utilities
# ============================================================

@blog_bp.route("/config", methods=["GET"])
@require_blog_access
def get_config():
    """Get blog editor configuration."""
    return jsonify({
        "block_types": ALLOWED_BLOCK_TYPES,
        "limits": EDITOR_LIMITS,
        "allowed_fonts": EDITOR_LIMITS["allowed_fonts"],
    })


@blog_bp.route("/check-slug", methods=["GET"])
@require_blog_access
def check_slug():
    """Check if a slug is available."""
    workspace_id = request.args.get("workspace_id", type=int)
    slug = request.args.get("slug", "").strip()
    exclude_id = request.args.get("exclude_id", type=int)
    
    if not workspace_id or not slug:
        return jsonify({"error": "workspace_id and slug are required"}), 400
    
    query = BlogPost.query.filter_by(workspace_id=workspace_id, slug=slug)
    if exclude_id:
        query = query.filter(BlogPost.id != exclude_id)
    
    existing = query.first()
    
    if existing:
        suggested = BlogPost.generate_unique_slug(workspace_id, slug, exclude_id)
        return jsonify({"available": False, "suggested": suggested})
    
    return jsonify({"available": True, "slug": slug})


@blog_bp.route("/upload", methods=["POST"])
@require_blog_access
def upload_media():
    """Upload media for blog posts."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files["file"]
    workspace_id = request.form.get("workspace_id")
    
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400
    
    # Validate file size
    file.seek(0, 2)
    size_mb = file.tell() / (1024 * 1024)
    file.seek(0)
    
    allowed_extensions = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    
    if ext not in allowed_extensions:
        return jsonify({"error": f"File type not allowed. Allowed: {allowed_extensions}"}), 400
    
    if size_mb > EDITOR_LIMITS["max_image_size_mb"]:
        return jsonify({"error": f"File too large. Max: {EDITOR_LIMITS['max_image_size_mb']}MB"}), 400
    
    # In production, upload to cloud storage (S3, GCS, etc.)
    # For now, return a placeholder URL
    # TODO: Implement actual file upload to cloud storage
    
    import uuid
    filename = f"{uuid.uuid4().hex}.{ext}"
    url = f"/uploads/blog/{workspace_id}/{filename}"
    
    return jsonify({
        "url": url,
        "filename": filename,
        "size_mb": round(size_mb, 2)
    })
